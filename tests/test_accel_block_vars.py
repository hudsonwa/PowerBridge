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

"""Native ``Vars:`` inside a block, and the compile-time Average refusal (#128).

The codegen makes a variable declared inside ``begin ... end`` a plain local
of the bar function: ``name = <init>`` each time the declaration runs (no
``f32()``), no trace column, nothing kept between bars. Redeclaring a
top-level scalar there is a no-op. The kernel runs both natively and must
give Python's fill digest and trace; uses Python cannot run (before the
declaration, outside its block, as a series) are refused at compile time.

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
    explain_native,
    fallback_count,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.test_accel_trace_parity import assert_trace_parity  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
FIXTURES = os.path.join(REPO, "accel", "src", "strategy", "fixtures")

with open(os.path.join(FIXTURES, "block_vars.txt"), encoding="utf-8") as _handle:
    BLOCK_VARS = _handle.read()

# Locals of every kind: a float literal kept in double precision, an int that
# reaches an int accumulator, a string and a true/false local, an init that is
# an expression, and a declaration inside Once and inside a nested loop.
KINDS = """
Vars: n(0), Hits(0), Lbl(""), P(0), i(0), j(0);
n = n + 1;
Once Begin
    Vars: Seed(7);
    Hits = Hits + Seed;
End;
If Close > Close[1] Then Begin
    Vars: Third(0.1), Step(2), Name("U"), Up(true), Mid((High + Low) / 2);
    Third = Third + Close / 3;
    Hits = Hits + Step;
    Lbl = Name + NumToStr(Third - IntPortion(Third), 3);
    If Up And Mid > Open Then P = Mid - Third;
End Else Begin
    Vars: Name("D");
    Lbl = Name;
End;
For i = 1 To 2 Begin
    For j = 1 To 2 Begin
        Vars: Acc(1);
        Acc = Acc * 2 + j + i;
        Hits = Hits + Acc;
    End;
End;
If Mod(n, 4) = 0 Then Buy (Lbl) Next Bar At Market
Else If Mod(n, 4) = 2 Then Sell ("X" + NumToStr(Hits, 0)) Next Bar At Market;
"""

PROGRAMS = {"block_vars": BLOCK_VARS, "kinds": KINDS}


@pytest.fixture(scope="module")
def bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_block_vars_compile_natively(name):
    assert explain_native(PROGRAMS[name]) == []


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, bars):
    source = PROGRAMS[name]
    before = fallback_count()
    fills = run_native(source, bars, NQ)
    assert fallback_count() == before, "the kernel fell back to Python"
    want = run_oracle(source, bars, NQ, max_bars_back=0)
    assert want["fills"] > 10, "the program must trade for the digest to mean anything"
    assert fills_sha256(fills) == want["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_trace_equals_python(name, bars):
    # Block locals have no trace column; the top-level variables they feed do.
    assert_trace_parity(PROGRAMS[name], bars)


def test_block_locals_are_reset_every_time_the_block_runs(bars):
    """Cnt is 1 on every bar that runs the block: nothing is kept."""
    signals = {f["signal"] for f in run_native(BLOCK_VARS, bars, NQ)}
    assert "B1.7750" in signals
    assert not any(s.startswith("B2.775") for s in signals)


REFUSED = {
    # Read where the declaration may not have run on this bar.
    "outside": (
        "Vars: A(0);\n"
        "If Close > Open Then Begin\n"
        "    Vars: Z(1);\n"
        "End;\n"
        "A = Z;\n",
        ("variable", "z", 5, "outside the block that declares it"),
    ),
    "before": (
        "Vars: A(0);\n"
        "If Close > Open Then Begin\n"
        "    A = Z;\n"
        "    Vars: Z(1);\n"
        "End;\n",
        ("variable", "z", 3, "outside the block that declares it"),
    ),
    "lookback": (
        "Vars: A(0);\n"
        "If Close > Open Then Begin\n"
        "    Vars: Z(1);\n"
        "    A = Z[1];\n"
        "End;\n",
        ("variable", "z", 4, "read as a series"),
    ),
    "series": (
        "Vars: A(0);\n"
        "If Close > Open Then Begin\n"
        "    Vars: Z(1);\n"
        "    A = Average(Z, 3);\n"
        "End;\n",
        ("variable", "z", 4, "read as a series"),
    ),
    # Python reloads a history variable's previous value here.
    "redeclared_history": (
        "Vars: A(0), B(0);\n"
        "B = A[1];\n"
        "If Close > Open Then Begin\n"
        "    Vars: A(5);\n"
        "    A = A + 1;\n"
        "End;\n",
        ("variable", "a", 4, "redeclared inside a block"),
    ),
    "input_name": (
        "Inputs: Len(3);\n"
        "Vars: A(0);\n"
        "If Close > Open Then Begin\n"
        "    Vars: Len(1);\n"
        "    A = Len;\n"
        "End;\n",
        ("variable", "len", 4, "shares its name with an input"),
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_uses_python_cannot_match_are_refused_at_compile_time(name, bars):
    source, (kind, var, line, words) = REFUSED[name]
    refusals = explain_native(source)
    assert [(r.kind, r.name, r.line) for r in refusals] == [(kind, var, line)]
    assert words in refusals[0].message
    with pytest.raises(AccelRefused, match=words):
        run_native(source, bars, NQ)


AVERAGE_UNKNOWN = """
Vars: x(0), A(0);
x = x + 0.5;
A = Average(x, 3);
if A > 4 then Buy next bar at market else Sell next bar at market;
"""


def test_explain_lists_the_average_unknown_type_refusal(bars):
    """#128: decided at compile time, so explain_native reports it."""
    refusals = explain_native(AVERAGE_UNKNOWN)
    if sys.version_info >= (3, 12):
        assert [(r.kind, r.name, r.line) for r in refusals] == [("function", "Average", 4)]
        assert "Python 3.12+ sums floats with compensation" in refusals[0].message
        with pytest.raises(AccelRefused, match="Average over a series"):
            run_native(AVERAGE_UNKNOWN, bars, NQ)
    else:
        assert refusals == []
        want = run_oracle(AVERAGE_UNKNOWN, bars, NQ, max_bars_back=0)["sha256"]
        assert fills_sha256(run_native(AVERAGE_UNKNOWN, bars, NQ)) == want
