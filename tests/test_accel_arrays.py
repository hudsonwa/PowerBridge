#!/usr/bin/env python3
# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Native arrays and ``for`` loops behave exactly like the Python runtime.

Each synthetic program runs through ``run_native`` (no fallback allowed) and
must give the same fill digest as the Python path. Loop-only programs must
also trace identically. Out-of-range indexes must fail both paths, batch and
parallel sweeps must start every variant with fresh arrays, and each array
feature the kernel does not run yet must be refused with a specific reason.
Skips cleanly when the extension is absent.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    accel_available,
    audit_skip_eval_from,
    fallback_count,
    run_native,
    run_native_inputs,
    run_native_sweep,
    run_native_trace,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.test_accel_trace_parity import assert_trace_parity  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
FIXTURES = os.path.join(REPO, "accel", "src", "strategy", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


ROLLING = """
Inputs: Len(5);
Arrays: Buf[10](0);
Vars: i(0), S(0);
for i = Len - 1 downto 1 begin Buf[i] = Buf[i - 1]; end;
Buf[0] = Close;
S = Array_Sum(Buf, 0, Len - 1);
if Buf[Len - 1] > 0 and Close > S / Len then Buy ("R" + NumToStr(S, 4)) next bar at market;
if Buf[Len - 1] > 0 and Close < S / Len then SellShort ("Q" + NumToStr(S, 4)) next bar at market;
"""

BOOL_AND_DYNAMIC = """
Arrays: Up[4](false), D[](0);
Vars: n(0), k(0), Ups(0), Top(0);
n = n + 1;
Up[Mod(n, 5)] = Close > Open;
Ups = 0;
for k = 0 to 4 begin if Up[k] then Ups = Ups + 1; end;
if Array_SetMaxIndex(D, Mod(n, 7)) then D[Mod(n, 7)] = High - Low;
Top = Array_Highest(D, 0, Array_GetMaxIndex(D));
if Ups >= 3 then Buy ("U" + NumToStr(Top, 2)) next bar at market;
if Ups <= 1 then Sell ("V" + NumToStr(Top, 2)) next bar at market;
"""

# Element copies keep the element's own int/float type, so Array_Sum is exact
# on Python 3.12+ too (compensated float summation).
COPIES = """
Arrays: A[6](0), B[6](0.0);
Vars: i(0), T(0), U(0);
A[0] = 1;
for i = 5 downto 1 begin A[i] = A[i - 1]; B[i] = B[i - 1]; end;
B[0] = Close / 3;
T = Array_Sum(A, 0, 5) + Array_Sum(B, 0, 5);
U = Array_Lowest(B, 1, 5) + Array_IndexOf(A, 1) + Array_IndexOf(B, 0);
if T > U then Buy ("C" + NumToStr(T, 6) + "/" + NumToStr(U, 6)) next bar at market;
if T < U + 10000 then Sell ("D" + NumToStr(T, 6)) next bar at market;
"""

# Python 3.12+ sums floats with compensation (S is 1.0, not 0.0) but adds a
# later int plainly (T stays 0.0); the kernel follows the running Python.
SUM_PRECISION = """
Arrays: B[3](0.0), C[3](0);
Vars: n(0), S(0), T(0);
n = n + 1;
B[0] = 10000000000000000.0; B[1] = 1.0; B[2] = -10000000000000000.0; B[3] = Close - Close;
C[0] = 10000000000000000.0; C[1] = 1; C[2] = -10000000000000000.0;
S = Array_Sum(B, 0, 3);
T = Array_Sum(C, 0, 2);
if Mod(n, 2) = 0 then Buy ("S" + NumToStr(S, 1) + "T" + NumToStr(T, 1)) next bar at market
else Sell ("X" + NumToStr(S, 1)) next bar at market;
"""

# The bar loop is bar-close only, so IntraBarPersist changes nothing.
INTRABAR = """
Arrays: IntraBarPersist P[2](1);
P[0] = P[0] + 1;
P[2] = P[2] + Close - Open;
if Mod(P[0], 2) = 0 then Buy ("P" + NumToStr(P[2], 2)) next bar at market
else Sell ("Q" + NumToStr(P[0], 0)) next bar at market;
"""

# Mod and division inside a loop follow Python's sign and float rules, and
# Close[k] with a loop index reads the unrounded lookback.
LOOP_ARITHMETIC = """
Arrays: R[5](0.0);
Vars: k(0), n(0), Acc(0), Q(0);
n = n + 1;
Acc = 0;
for k = 0 to 4 begin
  R[k] = Mod(Close[k] - Open[k] - k, 3) + (High[k] - Low[k]) / (k + 1);
  Acc = Acc + Mod(-n - k, 4) / 7 + Close[k] / Close;
end;
Q = Array_Sum(R, 0, 4);
if Mod(n, 3) = 1 and Acc > 5 then Buy ("M" + NumToStr(Q, 6) + "/" + NumToStr(Acc, 8)) next bar at market;
if Mod(n, 3) = 2 then Sell ("N" + NumToStr(Q, 6)) next bar at market;
"""

SYNTHETIC = {
    "arrays_numeric": fixture("arrays_numeric.txt"),
    "loops_only": fixture("loops_only.txt"),
    "rolling": ROLLING,
    "bool_and_dynamic": BOOL_AND_DYNAMIC,
    "copies": COPIES,
    "sum_precision": SUM_PRECISION,
    "intrabar_persist": INTRABAR,
    "loop_arithmetic": LOOP_ARITHMETIC,
}

# Loop-only programs: no array columns, so the typed trace must match too.
LOOP_TRACES = {
    "loops_only": fixture("loops_only.txt"),
    "counter_loops": """
Inputs: Len(3);
Vars: i(0), n(0), Seen(0), Odd(false);
n = n + 1;
Seen = 0.0;
for i = 0 to Len + 1 begin
  Odd = Mod(i, 2) = 1;
  if Odd then continue;
  Seen = Seen + i * Close;
end;
if Seen > 4 * Close then Buy next bar at market else Sell next bar at market;
""",
    "loop_lookbacks": """
Vars: k(0), n(0), Swing(0), Rem(0);
n = n + 1;
Swing = 0;
Rem = 0;
for k = 1 to 6 begin
  Swing = Swing + (Close[k - 1] - Close[k]) / k;
  Rem = Rem + Mod(Close[k] * 4 - n, 7);
end;
if Swing > 0 and Mod(Rem, 2) < 1 then Buy next bar at market else Sell next bar at market;
""",
}

# The loop may not run, so `i` is the f32-seeded float on some bars and the
# loop's int on others: Python's type depends on the run.
EMPTY_RANGE = """
Vars: i(0), n(0), Seen(0);
n = n + 1;
for i = 3 to Mod(n, 5) begin Seen = Seen + i; end;
if Seen > 20 then Buy next bar at market else Sell next bar at market;
"""


@pytest.fixture(scope="module")
def bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


def native_sha(source, bars):
    before = fallback_count()
    fills = run_native(source, bars, NQ)
    assert fallback_count() == before, "the kernel fell back to Python"
    return fills, fills_sha256(fills)


@pytest.mark.parametrize("name", sorted(SYNTHETIC))
def test_native_digest_equals_python(name, bars):
    source = SYNTHETIC[name]
    fills, sha = native_sha(source, bars)
    python = run_oracle(source, bars, NQ)
    assert len(fills) > 10, "the fixture must trade"
    assert sha == python["sha256"]


@pytest.mark.parametrize("name", sorted(LOOP_TRACES))
def test_loop_programs_trace_like_python(name, bars):
    assert_trace_parity(LOOP_TRACES[name], bars)


def test_loop_that_may_not_run_refuses_the_trace(bars):
    with pytest.raises(AccelRefused, match=r"trace of i: .*\(int or float\)"):
        run_native_trace(EMPTY_RANGE, bars, NQ)
    _, sha = native_sha(EMPTY_RANGE, bars)
    assert sha == run_oracle(EMPTY_RANGE, bars, NQ)["sha256"]


def test_array_programs_refuse_the_trace(bars):
    with pytest.raises(AccelRefused, match="trace of array buf: array columns"):
        run_native_trace(ROLLING, bars, NQ)


def test_sum_precision_follows_the_running_python(bars):
    fills, _ = native_sha(SUM_PRECISION, bars)
    want = "S1.0T0.0" if sys.version_info >= (3, 12) else "S0.0T0.0"
    assert {fill["signal"] for fill in fills if fill["signal"].startswith("S")} == {want}


def test_sum_of_elements_of_unknown_type_needs_python_before_312(bars):
    """An input's int/float type depends on the caller; 3.12+ refuses."""
    source = """
Inputs: Len(3);
Arrays: B[2](0.5);
Vars: S(0);
B[0] = Len;
S = Array_Sum(B, 0, 2);
if S > 4 then Buy next bar at market else Sell next bar at market;
"""
    if sys.version_info >= (3, 12):
        with pytest.raises(AccelRefused, match="Python 3.12\\+ sums floats"):
            run_native(source, bars, NQ)
    else:
        assert native_sha(source, bars)[1] == run_oracle(source, bars, NQ)["sha256"]


def test_out_of_range_index_fails_both_paths(bars):
    source = fixture("arrays_oob.txt")
    with pytest.raises(AccelRefused, match="array index 6 out of range for buf"):
        run_native(source, bars, NQ)
    with pytest.raises(IndexError):
        run_el(source, bars, NQ)


@pytest.mark.parametrize("threads", [None, 2])
def test_sweeps_start_every_variant_with_fresh_arrays(threads, bars):
    variants = [{"Len": 4}, {"Len": 6}, {"Len": 4}, {}]
    solo = [run_native_inputs(ROLLING, bars, NQ, v) for v in variants]
    batch = run_native_sweep(ROLLING, bars, NQ, variants, threads=threads)
    assert [fills_sha256(f) for f in batch] == [fills_sha256(f) for f in solo]
    assert fills_sha256(solo[0]) != fills_sha256(solo[1])
    assert fills_sha256(solo[3]) == native_sha(ROLLING, bars)[1]


def test_array_programs_skip_no_prefix(bars):
    dates = [bar["date"] for bar in bars]
    start, skipped, reason = audit_skip_eval_from(ROLLING, dates)
    assert (start, skipped) == (0, False)
    assert reason == "arrays or for loops; cannot skip a prefix"


REFUSED = {
    "string arrays": 'Arrays: S[3]("");\nif Close > 0 then Buy next bar at market;',
    "array b size is not an integer literal":
        "Inputs: N(3);\nArrays: B[N](0);\nif Close > 0 then Buy next bar at market;",
    "array function array_sort is not native yet":
        "Arrays: B[3](0);\nArray_Sort(B, 0, 3, true);",
    "array function array_gettype is not native yet":
        "Arrays: B[3](0);\nVars: T(0);\nT = Array_GetType(B);",
    "array function variancearray is not native yet":
        "Arrays: B[3](0);\nVars: T(0);\nT = VarianceArray(B, 3);",
    "array function array_compare is not native yet":
        "Arrays: B[3](0), C[3](0);\nVars: T(0);\nT = Array_Compare(B, 0, C, 0, 2);",
    "array b used as a whole outside a native Array_\\* function":
        "Arrays: B[3](0);\nVars: T(0);\nT = MaxList(B, 1);",
    "array_sum argument 2 is not an int in Python":
        "Arrays: B[3](0);\nVars: T(0);\nT = Array_Sum(B, Close, 2);",
    "while loops": "Vars: i(0);\nwhile i < 3 begin i = i + 1; end;",
}


@pytest.mark.parametrize("reason", sorted(REFUSED))
def test_unsupported_array_features_are_refused(reason, bars):
    before = fallback_count()
    with pytest.raises(AccelRefused, match=reason):
        run_native(REFUSED[reason], bars, NQ)
    assert fallback_count() == before + 1


def test_gt_master_now_stops_at_while_loops(bars):
    path = os.path.join(REPO, "ground_truth", "GT_master_strategy.txt")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    with pytest.raises(AccelRefused, match="while loops"):
        run_native(source, bars, NQ)


# Python raises on these, on a bar that cannot trade: the kernel must refuse
# (not skip the bar as a prefix, nor drop the unread result as dead code).
RAISING_IN_LOOPS = {
    "zero divisor in a loop": (
        "Vars: k(0), U(0);\nfor k = 0 to 2 begin U = Close / k; end;\n",
        ZeroDivisionError,
    ),
    "modulo by zero of an element": (
        "Arrays: B[2](0);\nVars: k(0), U(0);\n"
        "for k = 0 to 2 begin U = Mod(Close, B[k]); end;\n",
        ZeroDivisionError,
    ),
    "array element divisor": (
        "Arrays: B[2](1);\nVars: U(0);\nB[1] = B[1] - 1;\nU = Close / B[1];\n",
        ZeroDivisionError,
    ),
}


@pytest.mark.parametrize("name", sorted(RAISING_IN_LOOPS))
def test_python_errors_in_loops_and_arrays_refuse(name, bars):
    body, error = RAISING_IN_LOOPS[name]
    source = body + f"If Date >= {bars[-1]['date']} Then Buy next bar at market;\n"
    with pytest.raises(error):
        run_oracle(source, bars, NQ)
    with pytest.raises(AccelRefused, match="the Python path raises"):
        run_native(source, bars, NQ)


def test_arrays_and_loops_match_python_on_a_long_random_walk():
    """20k bars: arrays and loops stay in step with Python over a long tape."""
    from tests.test_accel_lookback_precision import random_walk

    long_bars = random_walk(20000, seed=7)
    source = LOOP_ARITHMETIC
    fills, sha = native_sha(source, long_bars)
    assert len(fills) > 100, "the program must trade on the long tape"
    assert sha == run_oracle(source, long_bars, NQ)["sha256"]
