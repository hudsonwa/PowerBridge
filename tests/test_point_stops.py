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

"""test_point_stops.py — #34: Fill_Array, SetDollarTrailing and the Set*_pt stops.

SetStopLoss_pt / SetProfitTarget_pt / SetBreakEven_pt / SetPercentTrailing_pt /
SetTrailingStop_pt are the MultiCharts point-based forms of the dollar stops:
the amount is in price points, so a `_pt` run must trade exactly like the
dollar run given `points * BigPointValue` (NQ: 20). Each case checks that on a
synthetic tape, that the stop actually changes the trade list, and (when the
native extension is built) that the native kernel agrees with Python.
SetDollarTrailing's exit price is checked exactly on hand-built bars.

Stdlib + pytest only, synthetic bars, no captures.
"""
import math
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.engine import FillEngine  # noqa: E402
from pl_transpiler.runtime._rt_datetime import pl_fill_array  # noqa: E402
from pl_transpiler.runtime.accel import AccelRefused, run_native  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from tests.conformance_harness import trade_difference  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")
PTS = 6.0  # points; the dollar twin uses PTS * NQ.bigpointvalue


def _tape(n=240):
    """A deterministic wavy tape with real ranges, so every stop can trigger."""
    bars = []
    for i in range(n):
        mid = 15000.0 + 40.0 * math.sin(i / 9.0) + 15.0 * math.sin(i / 2.3)
        open_ = round(mid * 4) / 4
        close = round((mid + 3.0 * math.cos(i / 1.7)) * 4) / 4
        bars.append({
            "date": 1240102 + i // 60,
            "time": 930 + (i % 60),
            "open": open_,
            "high": max(open_, close) + 2.5,
            "low": min(open_, close) - 2.5,
            "close": close,
            "volume": 1000.0,
        })
    return bars


BARS = _tape()


def _el(risk_stmt):
    return f"""
If MarketPosition = 0 and Close > Close[1] Then Buy ("LE") next bar at market ;
If MarketPosition = 0 and Close < Close[1] Then SellShort ("SE") next bar at market ;
If BarsSinceEntry > 25 Then Begin
    Sell ("LX") next bar at market ;
    BuyToCover ("SX") next bar at market ;
End ;
{risk_stmt}
"""


def _trades(el):
    return run_oracle(el, BARS, NQ)["records"]


DOLLARS = PTS * NQ.bigpointvalue
CASES = [
    ("SetStopLoss_pt", f"SetStopLoss_pt( {PTS} ) ;", f"SetStopLoss( {DOLLARS} ) ;"),
    ("SetProfitTarget_pt", f"SetProfitTarget_pt( {PTS} ) ;", f"SetProfitTarget( {DOLLARS} ) ;"),
    ("SetBreakEven_pt", f"SetBreakEven_pt( {PTS} ) ;", f"SetBreakEven( {DOLLARS} ) ;"),
    ("SetPercentTrailing_pt", f"SetPercentTrailing_pt( {PTS}, 50 ) ;",
     f"SetPercentTrailing( {DOLLARS}, 50 ) ;"),
    ("SetTrailingStop_pt", f"SetTrailingStop_pt( {PTS} ) ;", f"SetDollarTrailing( {DOLLARS} ) ;"),
]


@pytest.mark.parametrize("name,pt_stmt,dollar_stmt", CASES, ids=[c[0] for c in CASES])
def test_point_stop_matches_dollar_twin(name, pt_stmt, dollar_stmt):
    baseline = _trades(_el(""))
    pt = _trades(_el(pt_stmt))
    assert pt == _trades(_el(dollar_stmt))
    assert pt != baseline, f"{name} never fired on the synthetic tape"


def _held(risk, long_pos=True, entry=100.0):
    """A FillEngine holding one NQ contract entered on bar 1 at ``entry``."""
    fe = FillEngine(config=NQ)
    fe.market_position = 1 if long_pos else -1
    fe.contracts = 1
    fe.entry_price = entry
    fe.entry_bar = 1
    fe._risk.update(fe._resolve_point_risk(risk))
    return fe


def _exit_px(fe):
    assert fe.market_position == 0, "the trail did not fire"
    return fe.closed_trades[-1]["exit_px"]


def test_dollar_trailing_long_ratchets_inside_bar():
    # Open 100 is nearer the low: low 99 is above the $60 (3 pt) stop at 97,
    # then the high 110 lifts the peak to $200 and the trail to 107, and the
    # fall to the 104 close crosses it -> exit at 107 (+$140).
    fe = _held({"dollar_trailing": 60})
    fe._check_money_management_stops(110.0, 99.0, 104.0, 3, 100.0)
    assert _exit_px(fe) == 107.0
    assert fe.net_profit == 140.0


def test_dollar_trailing_short_ratchets_inside_bar():
    # Short at 100: high 101 stays under the stop at 103, the low 90 drops the
    # trail to 93, and the rise to the 96 close crosses it -> exit at 93.
    fe = _held({"dollar_trailing": 60}, long_pos=False)
    fe._check_money_management_stops(101.0, 90.0, 96.0, 3, 100.0)
    assert _exit_px(fe) == 93.0
    assert fe.net_profit == 140.0


def test_dollar_trailing_uses_prior_bar_peak_and_gaps_at_open():
    # A prior-bar peak of $200 holds the long trail at 107; a gap open at 105
    # is already through it, so the exit fills at the open.
    fe = _held({"dollar_trailing": 60})
    fe._peak_profit = 200.0
    fe._check_money_management_stops(106.0, 104.0, 105.0, 3, 105.0)
    assert _exit_px(fe) == 105.0


def test_dollar_trailing_snaps_away_from_position():
    # From a $200 peak a long trail sits at 100 + (200 - amount) / 20:
    # $50 -> 107.5 and $55 -> 107.25 are on the tick grid; $57 -> 107.15 is
    # floored to 107.0.
    for amount, level in ((50, 107.5), (55, 107.25), (57, 107.0)):
        fe = _held({"dollar_trailing": amount})
        fe._peak_profit = 200.0
        fe._check_money_management_stops(108.5, 101.0, 101.0, 3, 108.0)
        assert _exit_px(fe) == level, amount


def test_dollar_trailing_holds_when_not_crossed():
    fe = _held({"dollar_trailing": 60})
    fe._check_money_management_stops(110.0, 99.0, 108.0, 3, 100.0)
    assert fe.market_position == 1
    assert fe._peak_profit == 200.0


def test_trailing_stop_pt_is_points_times_big_point_value():
    fe = _held({"trailing_stop_pt": 3})
    assert fe._risk["dollar_trailing"] == 3 * NQ.bigpointvalue
    fe._check_money_management_stops(110.0, 99.0, 104.0, 3, 100.0)
    assert _exit_px(fe) == 107.0


def test_dollar_trailing_changes_the_trade_list():
    # MM exits are not fill records, but freeing the position lets the next
    # entry come earlier, so an armed trail must change the trades.
    assert _trades(_el(f"SetDollarTrailing( {DOLLARS} ) ;")) != _trades(_el(""))


def _accel_or_skip():
    try:
        import pl_transpiler._accel  # noqa: F401
    except ImportError:
        pytest.skip(ACCEL_SKIP)


@pytest.mark.needs_native
@pytest.mark.parametrize("name,pt_stmt,dollar_stmt", CASES, ids=[c[0] for c in CASES])
def test_point_stop_native_matches_python(name, pt_stmt, dollar_stmt):
    _accel_or_skip()
    el = _el(pt_stmt)
    try:
        native = run_native(el, BARS, NQ)
    except AccelRefused as exc:  # pragma: no cover - a refusal is a failure here
        pytest.fail(f"native kernel refused {name}: {exc}")
    assert native, f"{name}: native run produced no trades"
    assert trade_difference(_trades(el), native) is None


BOTH = [
    ("SetStopLoss_pt", f"SetStopLoss( {DOLLARS * 3} ) ; SetStopLoss_pt( {PTS} ) ;",
     f"SetStopLoss( {DOLLARS} ) ;"),
    ("SetProfitTarget_pt", f"SetProfitTarget( {DOLLARS * 3} ) ; SetProfitTarget_pt( {PTS} ) ;",
     f"SetProfitTarget( {DOLLARS} ) ;"),
]


@pytest.mark.parametrize("name,both_stmt,dollar_stmt", BOTH, ids=[c[0] for c in BOTH])
def test_point_form_wins_over_dollar_form_on_same_bar(name, both_stmt, dollar_stmt):
    # Setting a _pt form and its dollar twin on one bar: the _pt amount wins,
    # and the native kernel agrees.
    el = _el(both_stmt)
    py = _trades(el)
    assert py == _trades(_el(dollar_stmt))
    assert py != _trades(_el(f"SetStopLoss( {DOLLARS * 3} ) ; SetProfitTarget( {DOLLARS * 3} ) ;"))
    try:
        import pl_transpiler._accel  # noqa: F401
    except ImportError:
        return
    assert trade_difference(py, run_native(el, BARS, NQ)) is None


def test_fill_array_one_dimensional():
    arr = [3, 3, 3, 3]
    pl_fill_array(arr, 7)
    assert arr == [7, 7, 7, 7]


def test_fill_array_multi_dimensional():
    grid = [[1, 2, 3], [4, 5, 6]]
    pl_fill_array(grid, 0.5)
    assert grid == [[0.5] * 3, [0.5] * 3]


def test_fill_array_in_strategy():
    el = """
Vars: Tot(0) ;
Arrays: FArr[4](3) ;
Fill_Array( FArr, 7 ) ;
If FArr[0] + FArr[4] = 14 and MarketPosition = 0 Then Buy next bar at market ;
"""
    assert _trades(el), "Fill_Array did not set the array elements"
