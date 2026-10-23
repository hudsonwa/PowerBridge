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

"""Native Value0..Value99 / Condition0..Condition99 behave like the Python path.

EL predeclares them as numeric / boolean variables that start at 0 / false
and keep their value from bar to bar, exactly like ``Vars: Value1(0),
Condition1(false);``. Each synthetic program runs through ``run_native`` (no
fallback allowed) and must give the Python fill digest. Skips cleanly when
the extension is absent.
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
    run_native,
    run_native_trace,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import format_trace_cell, load_bars_csv, run_el  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

# A scratch variable as a for-loop counter and as an array index.
LOOP_COUNTER = """
Arrays: Buf[10](0);
for Value1 = 0 to 3 begin Buf[Value1] = Close[Value1]; end;
Value2 = Buf[0] - Buf[3];
if Value2 > 0 then Buy ("L" + NumToStr(Value2, 2) + "/" + NumToStr(Value1, 0)) next bar at market;
if Value2 < 0 then SellShort ("S" + NumToStr(Value2, 2)) next bar at market;
"""

# Conditions in if statements; mixed-case spellings name one variable.
CONDITIONS = """
Condition1 = Close > Open;
CONDITION2 = Close[1] > Open[1];
Value0 = (High + Low) / 2;
value99 = VALUE0 * 3;
if condition1 and not Condition2 then Buy ("B" + NumToStr(Value99, 4)) next bar at market
else if Condition2 then Sell ("X" + NumToStr(Value0, 4)) next bar at market;
"""

# Downto loop with a scratch index into a declared accumulator array.
DOWNTO_INDEX = """
Arrays: Rng[5](0);
Vars: n(0);
n = n + 1;
for Value5 = 5 downto 1 begin Rng[Value5] = Rng[Value5 - 1]; end;
Rng[0] = High - Low;
Value6 = 0;
for Value7 = 0 to 4 begin
    if Rng[Value7] > Value6 then Value6 = Rng[Value7];
end;
Condition10 = Mod(n, 3) = 0;
if Condition10 and Value6 > Rng[0] then Buy ("D" + NumToStr(Value6, 2)) next bar at market;
if Condition10 = false then SellShort ("E" + NumToStr(Value7, 0)) next bar at market;
"""

# A declared ValueN is an ordinary persisted variable.
DECLARED = """
Vars: Value1(0);
Value1 = Value1 + 1;
Value2 = Value1 * 2;
if Mod(Value1, 4) = 0 then Buy ("V" + NumToStr(Value2, 0)) next bar at market
else Sell next bar at market;
"""

# Read before write starts at 0 / false; Value1 = Value1 + 1 accumulates.
ACCUMULATE = """
if Value1 = 0 and Condition1 = false then Buy ("first") next bar at market;
Value1 = Value1 + 1;
if Mod(Value1, 5) = 0 then Buy ("B" + NumToStr(Value1, 0)) next bar at market
else if Mod(Value1, 5) = 2 then Sell next bar at market;
"""

# A condition keeps its value across bars until written again.
PERSIST = """
if Mod(CurrentBar, 7) = 0 then Condition2 = not Condition2;
if Condition2 then Buy ("P" + NumToStr(CurrentBar, 0)) next bar at market
else Sell next bar at market;
"""

# History and series arguments work as on a declared variable.
HISTORY = """
Value3 = Close;
Condition4 = Close > Open;
if Value3 > Average(Value3, 5) and Condition4[1] then Buy next bar at market;
if Value3[2] > Value3 then Sell next bar at market;
"""

# Only the order prefix: a program with no orders until a late bar.
LATE = """
Value8 = Value8 + 1;
if Value8 > 100 then Buy ("L" + NumToStr(Value8, 0)) next bar at market;
"""

PROGRAMS = {
    "loop_counter": LOOP_COUNTER,
    "conditions": CONDITIONS,
    "downto_index": DOWNTO_INDEX,
    "declared": DECLARED,
    "accumulate": ACCUMULATE,
    "persist": PERSIST,
    "history": HISTORY,
    "late": LATE,
}


@pytest.fixture(scope="module")
def bars():
    return load_bars_csv(BARS_CSV)


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, bars):
    source = PROGRAMS[name]
    fills = run_native(source, bars, NQ)
    assert fills, f"{name}: no fills, the program exercises nothing"
    assert fills_sha256(fills) == run_oracle(source, bars, NQ)["sha256"]


def test_undeclared_matches_explicit_declaration(bars):
    """An undeclared ValueN/ConditionN is `Vars: ValueN(0), ConditionN(false);`."""
    declared = "Vars: Value1(0), Condition1(false);\n" + ACCUMULATE
    want = run_oracle(declared, bars, NQ)["sha256"]
    assert run_oracle(ACCUMULATE, bars, NQ)["sha256"] == want
    assert fills_sha256(run_native(ACCUMULATE, bars, NQ)) == want


@pytest.mark.parametrize("name", ["conditions", "accumulate", "history"])
def test_native_trace_equals_python(name, bars):
    """Scratch variables are trace columns, as declared variables are."""
    source = PROGRAMS[name]
    columns, rows, _fills = run_native_trace(source, bars, NQ)
    expected = run_el(source, bars, NQ)
    assert columns == expected["columns"]
    assert "value1" in columns or "condition1" in columns or "value3" in columns
    assert [[format_trace_cell(c) for c in row] for row in rows] == expected["rows"]


def test_crosses_over_scratch_refused_like_declared(bars):
    """The kernel has no crosses over a variable yet; a scratch one is refused
    exactly as a declared one is, so the caller falls back to Python."""
    body = "Value3 = Close; if Value3 crosses above 100 then Buy next bar at market;"
    for source in (body, "Vars: Value3(0); " + body):
        with pytest.raises(AccelRefused, match="expected a series"):
            run_native(source, bars, NQ)
    assert run_oracle(body, bars, NQ)["sha256"] == run_oracle(
        "Vars: Value3(0); " + body, bars, NQ)["sha256"]
