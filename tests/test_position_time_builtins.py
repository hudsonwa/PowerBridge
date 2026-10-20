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
"""test_position_time_builtins.py — MaxShares, MaxSharesHeld, OpenEntriesCount,
MillisecondsFromDateTime and SessionStartDay (issue #36).

Synthetic tape: bar k (1-based) opens at 99 + k. Market orders fill at the next
bar's open, so the strategy below holds:

    position   bars     entries                      max contracts
    A          3..5     LA 1 (bar 3), LB 2 (bar 4)    3
    B          8..9     LC 1                          1
    C          12..14   SA 4 (short, still open)      4

TickID and ExecOffset are not derivable from bar data and stay loud stubs.

Stdlib only, no captures.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from pl_transpiler.runtime import pl_runtime                             # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config           # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle                     # noqa: E402
from pl_transpiler.tools.pl_run import run_el                            # noqa: E402

NQ = get_config("NQ")

POSITION_EL = """Vars: N(1), MS(0), MS1(0), MS2(0), MSN(0), MSH(0), OEC(0);
If BarNumber = 2 Then Buy ("LA") 1 contracts next bar at Market;
If BarNumber = 3 Then Buy ("LB") 2 contracts next bar at Market;
If BarNumber = 5 Then Sell ("LX") next bar at Market;
If BarNumber = 7 Then Buy ("LC") 1 contracts next bar at Market;
If BarNumber = 9 Then Sell ("LX2") next bar at Market;
If BarNumber = 11 Then SellShort ("SA") 4 contracts next bar at Market;
MS = MaxShares; MS1 = MaxShares(1); MS2 = MaxShares(2); MSN = MaxShares(N);
MSH = MaxSharesHeld; OEC = OpenEntriesCount;
"""

# bar -> (MaxShares, MaxShares(1), MaxShares(2), MaxSharesHeld, OpenEntriesCount)
EXPECTED = {
    1: (0, 0, 0, 0, 0), 2: (0, 0, 0, 0, 0),
    3: (1, 0, 0, 1, 1), 4: (3, 0, 0, 3, 2), 5: (3, 0, 0, 3, 2),
    6: (0, 3, 0, 3, 0), 7: (0, 3, 0, 3, 0),
    8: (1, 3, 0, 3, 1), 9: (1, 3, 0, 3, 1),
    10: (0, 1, 3, 3, 0), 11: (0, 1, 3, 3, 0),
    12: (4, 1, 3, 4, 1), 13: (4, 1, 3, 4, 1), 14: (4, 1, 3, 4, 1),
}


def _bars(n=14):
    return [
        {"date": 1240101 + i, "time": 1000 + i, "open": 100.0 + i,
         "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i, "volume": 1.0}
        for i in range(n)
    ]


def _rows_by_bar():
    out = run_el(POSITION_EL, _bars(), NQ, trace_columns=None)
    cols = out["columns"]
    return {int(r[cols.index("_barnumber")]): {c: float(v) for c, v in zip(cols, r)}
            for r in out["rows"]}


ROWS = _rows_by_bar()


@pytest.mark.parametrize("bar", sorted(EXPECTED))
def test_maxshares(bar):
    ms, ms1, ms2, _msh, _oec = EXPECTED[bar]
    row = ROWS[bar]
    assert (row["ms"], row["ms1"], row["ms2"]) == (ms, ms1, ms2)
    assert row["msn"] == ms1  # a variable positions-ago argument


@pytest.mark.parametrize("bar", sorted(EXPECTED))
def test_maxsharesheld(bar):
    assert ROWS[bar]["msh"] == EXPECTED[bar][3]


@pytest.mark.parametrize("bar", sorted(EXPECTED))
def test_openentriescount(bar):
    assert ROWS[bar]["oec"] == EXPECTED[bar][4]


def test_position_builtins_are_not_unimplemented():
    pl_runtime.pl_unimplemented_reset()
    run_oracle(POSITION_EL, _bars(), NQ)
    hit = pl_runtime.pl_unimplemented_report()
    assert not {"pl_maxshares", "pl_maxsharesheld", "pl_openentriescount"} & set(hit)


@pytest.mark.parametrize("dt,ms", [
    (39448.6874479167, 500),          # 16:29:55.500
    (45000.0, 0),                     # midnight
    (45000.5 + 0.123 / 86400, 123),   # 12:00:00.123
    (45000.5 + 0.9996 / 86400, 0),    # rounds up to the next second
])
def test_millisecondsfromdatetime(dt, ms):
    assert pl_runtime.pl_millisecondsfromdatetime(dt) == ms


def test_millisecondsfromdatetime_in_strategy():
    el = """Vars: MsV(0);
MsV = MillisecondsFromDateTime( 39448.6874479167 );
If MsV = 500 and MarketPosition = 0 Then Buy next bar at market;
"""
    assert run_oracle(el, _bars(), NQ)["records"]


@pytest.mark.parametrize("num,day", [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4), (0, 0), (6, 0)])
def test_sessionstartday(num, day):
    assert pl_runtime.pl_sessionstartday(1, num) == day


def test_sessionstartday_matches_session_end_day():
    # Session 1 opens the evening before the day it ends on.
    assert pl_runtime.pl_sessionstartday(1, 1) == pl_runtime.pl_sessionendday(1, 1) - 1


@pytest.mark.parametrize("name", ["TickID", "ExecOffset"])
def test_bar_data_cannot_supply_these(name):
    pl_runtime.pl_unimplemented_reset()
    run_oracle(f"Vars: V(0); V = {name};\n", _bars(), NQ)
    assert pl_runtime.pl_unimplemented_report().get(f"pl_{name.lower()}", 0) > 0


GATED_EL = """
If MarketPosition = 0 and MaxSharesHeld < 2 Then Buy ("LE") 2 contracts next bar at market;
If MarketPosition = 0 and MaxSharesHeld >= 2 Then SellShort ("SE") 1 contracts next bar at market;
If BarsSinceEntry >= 2 Then Begin
    Sell ("LX") next bar at market;
    BuyToCover ("SX") next bar at market;
End;
"""


def test_maxsharesheld_native_matches_python():
    # MaxSharesHeld reads the same engine field as MaxContractsHeld, which the
    # native kernel already supplies; both paths must trade identically.
    try:
        import pl_transpiler._accel  # noqa: F401
    except ImportError:
        pytest.skip(ACCEL_SKIP)
    from pl_transpiler.runtime.accel import run_native
    from tests.conformance_harness import trade_difference
    py = run_oracle(GATED_EL, _bars(), NQ)["records"]
    # MaxSharesHeld flips the strategy from long to short after the first trade.
    assert {"LE", "SE"} <= {r["signal"] for r in py}
    assert trade_difference(py, run_native(GATED_EL, _bars(), NQ)) is None


PARTIAL_EXIT_EL = """Vars: OEC(0), CC(0);
If BarNumber = 2 Then Buy ("A") 1 contracts next bar at Market;
If BarNumber = 4 Then Buy ("B") 2 contracts next bar at Market;
If BarNumber = 6 Then Sell 1 contracts total next bar at Market;
If BarNumber = 8 Then Sell 1 contracts total next bar at Market;
OEC = OpenEntriesCount; CC = CurrentContracts;
"""


def test_openentriescount_drops_when_fifo_exit_closes_an_entry():
    # A (1) fills at bar 3 and B (2) at bar 5. The bar-7 exit uses up A, so one
    # entry is left; the bar-9 exit only shrinks B, so the count stays at 1.
    out = run_el(PARTIAL_EXIT_EL, _bars(10), NQ, trace_columns=None)
    cols = out["columns"]
    got = {int(float(r[cols.index("_barnumber")])):
           (int(float(r[cols.index("oec")])), int(float(r[cols.index("cc")])))
           for r in out["rows"]}
    assert got[4] == (1, 1)
    assert got[6] == (2, 3)
    assert got[7] == (1, 2)
    assert got[9] == (1, 1)
