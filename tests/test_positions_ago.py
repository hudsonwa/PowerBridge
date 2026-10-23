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

"""test_positions_ago.py — EntryDate(n) / ExitPrice(n) / ... read past positions.

Issue #9: the positions-ago argument of EntryDate/EntryTime/EntryPrice/ExitDate/
ExitTime/ExitPrice was discarded and the current-position scalar returned, so
EntryDate(n) was 0 for most n (EntryDate/EntryTime were never supplied at all).
n = 0 is the open position (0 when flat); n >= 1 is the n-th most recently
closed position; n past the first closed position is 0.

Synthetic tape: bar k (1-based) opens at 99 + k, date 1240100 + k, time 999 + k.
Market orders fill at the next bar's open, so the strategy below trades:

    position   entry (bar, date, time, px)    exit (bar, date, time, px)
    A          3, 1240103, 1002, 102           5, 1240105, 1004, 104
    B          7, 1240107, 1006, 106           9, 1240109, 1008, 108
    C         11, 1240111, 1010, 110           (open at the end)

Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import run_el  # noqa: E402

NQ = get_config("NQ")
N_BARS = 14

TRADES_EL = """Vars: N(2),
    ED0(0), ED1(0), ED2(0), ED3(0), ET0(0), ET1(0), ET2(0), ET3(0),
    EP0(0), EP1(0), EP2(0), EP3(0), XD0(0), XD1(0), XD2(0), XD3(0),
    XT1(0), XT2(0), XT3(0), XP1(0), XP2(0), XP3(0), EDN(0), XPN(0);
If BarNumber = 2 or BarNumber = 6 or BarNumber = 10 Then Buy ("LE") next bar at Market;
If BarNumber = 4 or BarNumber = 8 Then Sell ("LX") next bar at Market;
ED0 = EntryDate(0); ED1 = EntryDate(1); ED2 = EntryDate(2); ED3 = EntryDate(3);
ET0 = EntryTime(0); ET1 = EntryTime(1); ET2 = EntryTime(2); ET3 = EntryTime(3);
EP0 = EntryPrice(0); EP1 = EntryPrice(1); EP2 = EntryPrice(2); EP3 = EntryPrice(3);
XD0 = ExitDate(0); XD1 = ExitDate(1); XD2 = ExitDate(2); XD3 = ExitDate(3);
XT1 = ExitTime(1); XT2 = ExitTime(2); XT3 = ExitTime(3);
XP1 = ExitPrice(1); XP2 = ExitPrice(2); XP3 = ExitPrice(3);
EDN = EntryDate(N); XPN = ExitPrice(N);
"""


def _bars(n=N_BARS):
    return [
        {"date": 1240101 + i, "time": 1000 + i, "open": 100.0 + i,
         "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i, "volume": 1.0}
        for i in range(n)
    ]


def _rows_by_bar():
    out = run_el(TRADES_EL, _bars(), NQ, trace_columns=None)
    cols = out["columns"]
    return {int(r[cols.index("_barnumber")]): {c: float(v) for c, v in zip(cols, r)
                                               if not c.startswith("_")}
            for r in out["rows"]}


# Hand-computed from the table in the module docstring.
EXPECTED = {
    # Bar 10: flat, A and B closed (B most recent).
    10: {"ed0": 0, "ed1": 1240107, "ed2": 1240103, "ed3": 0,
         "et0": 0, "et1": 1006, "et2": 1002, "et3": 0,
         "ep0": 0, "ep1": 106, "ep2": 102, "ep3": 0,
         "xd0": 0, "xd1": 1240109, "xd2": 1240105, "xd3": 0,
         "xt1": 1008, "xt2": 1004, "xt3": 0,
         "xp1": 108, "xp2": 104, "xp3": 0,
         "edn": 1240103, "xpn": 104},
    # Bar 14: C open; B is one position ago, A two.
    14: {"ed0": 1240111, "ed1": 1240107, "ed2": 1240103, "ed3": 0,
         "et0": 1010, "et1": 1006, "et2": 1002, "et3": 0,
         "ep0": 110, "ep1": 106, "ep2": 102, "ep3": 0,
         "xd0": 0, "xd1": 1240109, "xd2": 1240105, "xd3": 0,
         "xt1": 1008, "xt2": 1004, "xt3": 0,
         "xp1": 108, "xp2": 104, "xp3": 0,
         "edn": 1240103, "xpn": 104},
    # Bar 6: flat after A; only one closed position.
    6: {"ed0": 0, "ed1": 1240103, "ed2": 0, "xd1": 1240105, "xd2": 0,
        "xt1": 1004, "xp1": 104, "xp2": 0, "edn": 0, "xpn": 0},
}


def test_positions_ago_values_match_hand_computed_trades():
    rows = _rows_by_bar()
    for bar, want in EXPECTED.items():
        got = {k: rows[bar][k] for k in want}
        assert got == {k: float(v) for k, v in want.items()}, f"bar {bar}: {got}"


def test_native_falls_back_and_matches_python():
    """The Rust kernel has no closed-position ledger, so it refuses ExitPrice(n)
    for n != 0 and pl_oracle falls back to the Python path: same digest."""
    el = ("If BarNumber = 2 Then Buy (\"LE\") next bar at Market;\n"
          "If BarNumber = 4 Then Sell (\"LX\") next bar at Market;\n"
          "If MarketPosition = 0 and ExitPrice(1) > 0 and BarNumber = 6 Then\n"
          "    Buy (\"LE2\") next bar at Market;\n")
    py = run_oracle(el, _bars(), NQ)
    native = run_oracle(el, _bars(), NQ, use_native=True)
    assert py["fills"] == 3, py["text"]
    assert native["text"] == py["text"]
