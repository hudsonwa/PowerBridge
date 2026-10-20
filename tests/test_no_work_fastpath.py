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

"""test_no_work_fastpath.py — the no-work flat-bar fast path.

The engine runs the full before_bar lifecycle every bar even when flat with no
pending/on-close order and no armed risk. This adds `FillEngine.no_work_state()`
(the exact predicate) and a fast path that skips that whole lifecycle, counting
hits in `fast_path_hits`.

These tests prove the fast path is EXACTLY the slow path: every transition
into/out of the no-work state is run both ways (fast path on, and forced off by
monkeypatching `no_work_state` -> False) and the engines must be field-for-field
identical — position, PnL, drawdown (peak_equity / max_id_drawdown), the fill
ledger, the closed-position ledger, and the pending/on-close/risk queues. Only
the counter itself may differ.

Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.engine import FillEngine, run_bar_loop              # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config         # noqa: E402

NQ = get_config("NQ")


def _snapshot(fe):
    """Every observable field a transition test must match field-for-field."""
    pos = fe.get_kwargs()
    pos["open_position_profit"] = fe.open_position_profit
    pos["entry_bar"] = fe.entry_bar
    pos["exit_bar"] = fe.exit_bar
    pos["peak_equity"] = fe.peak_equity
    pos["peak_profit"] = fe._peak_profit
    return {
        "pos": pos,
        "fills": [dict(f) for f in fe.fills],
        "closed_trades": [dict(t) for t in fe.closed_trades],
        "pending_orders": list(fe._pending_orders),
        "onclose_pending": [(o, px, bn, ds, ts) for (o, px, bn, ds, ts) in fe._onclose_pending],
        "risk": dict(fe._risk),
        "pos_mfe_pc": fe._pos_mfe_pc,
        "pos_mae_pc": fe._pos_mae_pc,
        "entry_prices": list(fe._entry_prices),
        "max_id_drawdown": fe.max_id_drawdown,
    }


def _run(scenario, fast=True, get_ohlcv=None):
    """Drive one tape through run_bar_loop with a scripted strategy.

    ``scenario`` is a list of per-bar dicts, each optionally carrying
    ``orders`` (a list of order tuples) and ``risk`` (a dict) returned by the
    strategy for that bar. ``fast=False`` forces the slow path by neutering
    ``no_work_state`` so the skip never fires. ``get_ohlcv`` overrides the
    default rising tape (so a loss/drawdown scenario can fall).
    """
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    if not fast:
        fe.no_work_state = lambda: False
    n = len(scenario)
    state = {"i": 0}

    def strategy(*_series, **kwargs):
        i = state["i"]
        state["i"] += 1
        item = scenario[i] if i < n else {}
        return {"orders": item.get("orders", []), "risk": item.get("risk", {})}

    dates = [1240102 + i for i in range(n)]
    times = [930 + (i % 60) for i in range(n)]

    def rising_ohlcv(i):
        px = 100.0 + i
        return (px, px + 2.0, px - 2.0, px + 1.0, 1.0)

    get_ohlcv = get_ohlcv or rising_ohlcv

    run_bar_loop(
        strategy,
        n_bars=n,
        get_ohlcv=get_ohlcv,
        dates=dates,
        times=times,
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    return fe


def _assert_identical(scenario, min_hits=1, get_ohlcv=None):
    fast = _run(scenario, fast=True, get_ohlcv=get_ohlcv)
    slow = _run(scenario, fast=False, get_ohlcv=get_ohlcv)
    assert _snapshot(fast) == _snapshot(slow), (
        "fast path diverted from slow path:\n  scenario=%r\n  fast=%r\n  slow=%r"
        % (scenario, _snapshot(fast), _snapshot(slow)))
    # The slow-path monkeypatch still runs the body, so its counter must be 0.
    assert slow.fast_path_hits == 0, slow.fast_path_hits
    if min_hits:
        assert fast.fast_path_hits >= min_hits, (
            f"expected >= {min_hits} flat no-work bars, got {fast.fast_path_hits}")
    return fast, slow


def test_predicate_flat_fresh_engine_is_no_work():
    fe = FillEngine(config=NQ)
    assert fe.no_work_state() is True
    assert fe.fast_path_hits == 0
    # Arming any risk, or queueing an order, flips it out of the no-work state.
    fe._risk["stop_loss"] = 500
    assert fe.no_work_state() is False
    fe._risk.clear()
    fe._pending_orders = [("buy", "market", 1, "next", "LE", None)]
    assert fe.no_work_state() is False
    fe._pending_orders = []
    fe._onclose_pending = [("x", 1.0, 1, 0, 0)]
    assert fe.no_work_state() is False
    fe._onclose_pending = []
    fe._risk["exit_on_close"] = True
    assert fe.no_work_state() is False
    fe._risk["exit_on_close"] = False
    # A flat engine with zero-valued risk settings is still no-work.
    fe._risk["profit_target"] = 0
    fe._risk["breakeven"] = 0
    assert fe.no_work_state() is True
    print("  [ok] predicate + counter flip on every ingress/egress condition")


def test_flat_prefix_is_fast_path_and_counter_counts():
    scenario = [{}] * 5
    fe, _slow = _assert_identical(scenario, min_hits=5)
    assert fe.fast_path_hits == 5, fe.fast_path_hits
    print("  [ok] an all-flat tape is 5 fast-path hits, identical to slow")


def test_entry_then_exit_transitions_out_and_back_in():
    # bar0: buy next bar (fast, flat). bar1: fill -> long. bar2: sell -> flat.
    # bar3+: flat again -> fast path resumes.
    scenario = [
        {"orders": [("buy", "market", 1, "next", "LE", None)]},
        {"orders": [("sell", "market", 1, "next", "LX", None)]},
        {},
        {},
        {},
    ]
    fe, _slow = _assert_identical(scenario, min_hits=2)
    assert fe.market_position == 0
    assert fe.total_trades == 1, fe.total_trades
    assert len(fe.fills) == 2, len(fe.fills)
    print("  [ok] entry+exit round-trip exits/renters the no-work state identically")


def test_drawdown_survives_a_losing_round_trip():
    # A long opened high and closed lower (falling tape) must fold the realized
    # LOSS into peak_equity and max_id_drawdown on the exit bar (a work bar),
    # then stay flat across subsequent no-work bars without losing that drawdown.
    def falling_ohlcv(i):
        px = 200.0 - i
        return (px, px + 1.0, px - 1.0, px - 0.5, 1.0)

    scenario = [
        {"orders": [("buy", "market", 1, "next", "LE", None)]},   # bar0
        {"orders": [("sell", "market", 1, "next", "LX", None)]},  # bar1: enters long
        {},                                                        # bar2: closes long (loss)
        {}, {}, {},
    ]
    fe, _slow = _assert_identical(scenario, min_hits=1, get_ohlcv=falling_ohlcv)
    assert fe.max_id_drawdown < 0.0, fe.max_id_drawdown
    assert fe.peak_equity > 0.0, fe.peak_equity
    # bars 0, 3, 4, 5 are flat no-work; 1 and 2 carry the fills.
    assert fe.fast_path_hits == 4, fe.fast_path_hits
    print("  [ok] realized drawdown survives across subsequent flat no-work bars")


def test_risk_arms_then_disarms_only_via_persistence():
    # SetStopLoss(500) keeps the bar out of no-work from the bar AFTER it is
    # set (risk is monotonic — the engine never unsets it). The bar that arms
    # the risk is itself still no-work (its before_bar runs before the strategy
    # returns the risk), matching the slow path field-for-field.
    scenario = [
        {"risk": {"stop_loss": 500}},
        {},
        {},
    ]
    fe, _slow = _assert_identical(scenario, min_hits=1)
    assert fe.fast_path_hits == 1, fe.fast_path_hits
    assert fe._risk.get("stop_loss") == 500
    print("  [ok] armed risk suppresses the fast path, identical to slow")


def test_onclose_order_transitions_out_of_no_work():
    # 'this bar on close' queues an on-close fill; the following bar applies it
    # (reversal to long) so it is out of no-work, then flattens only via a fresh
    # order. Every bar we do not force slow stays field-identical.
    scenario = [
        {"orders": [("buy", "close", 1, "this", "LE_CLS", None)]},
        {},
        {"orders": [("sell", "market", 1, "next", "LX", None)]},
        {},
        {},
    ]
    fe, _slow = _assert_identical(scenario, min_hits=1)
    assert fe.market_position == 0
    assert len(fe.fills) == 2, fe.fills
    print("  [ok] on-close entry transition is field-identical to the slow path")


def _rollover_tape_run(scenario, fast):
    """Run a multi-day tape so a real date change triggers the rollover path."""
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    if not fast:
        fe.no_work_state = lambda: False
    n = len(scenario)
    state = {"i": 0}

    def strategy(*_series, **kwargs):
        i = state["i"]
        state["i"] += 1
        item = scenario[i] if i < n else {}
        return {"orders": item.get("orders", []), "risk": item.get("risk", {})}

    # Two sessions: bars 0-1 day A, bars 2-3 day B.
    dates = [1240102, 1240102, 1240103, 1240103]
    times = [930, 931, 930, 931]

    def get_ohlcv(i):
        px = 100.0 + i
        return (px, px + 2.0, px - 2.0, px + 1.0, 1.0)

    run_bar_loop(
        strategy, n_bars=n, get_ohlcv=get_ohlcv, dates=dates, times=times,
        config=NQ, fill_engine=fe, emit_row=lambda *a: None, strict=True)
    return fe


def test_real_rollover_flattens_open_position_field_identical():
    # Buy next bar on bar0 (day A); fills bar1 (long). SetExitOnClose armed.
    # bar2 is a new day: rollover closes the long. Everything matches slow.
    scenario = [
        {"orders": [("buy", "market", 1, "next", "LE", None)], "risk": {"exit_on_close": True}},
        {},
        {},
        {},
    ]
    fast = _rollover_tape_run(scenario, fast=True)
    slow = _rollover_tape_run(scenario, fast=False)
    assert _snapshot(fast) == _snapshot(slow), (_snapshot(fast), _snapshot(slow))
    assert fast.market_position == 0 and fast.total_trades == 1
    # Entry plus the end-of-day exit (risk exits are in the fill log).
    assert [f['type'] for f in fast.fills] == ['EntryLong', 'ExitLong'], fast.fills
    print("  [ok] real session rollover flatten is field-identical")


def main():
    tests = [
        test_predicate_flat_fresh_engine_is_no_work,
        test_flat_prefix_is_fast_path_and_counter_counts,
        test_entry_then_exit_transitions_out_and_back_in,
        test_drawdown_survives_a_losing_round_trip,
        test_risk_arms_then_disarms_only_via_persistence,
        test_onclose_order_transitions_out_of_no_work,
        test_real_rollover_flattens_open_position_field_identical,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL NO-WORK FAST-PATH TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
