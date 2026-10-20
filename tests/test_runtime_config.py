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

"""
test_runtime_config.py — proves de-hardcoding is FUNCTIONAL (GOAL unit #4).

The chart/instrument constants that made the runner NQ-specific now live in a
per-run InstrumentConfig injected into the FillEngine — they are NOT baked into
the shared runtime as a hard-coded 20. This test falsifies that claim two ways:

  (a) Fill PnL responds to the injected config. Two FillEngines run the IDENTICAL
      order/price sequence; the only difference is the config's bigpointvalue. The
      realized net_profit must scale by exactly that ratio. If the engine ignored
      the config and used a hard-coded 20, the synthetic config's PnL would be
      wrong and the assertion fails.

  (b) The NQ config reproduces today's exact validated values (20 / 0.20 / 25 /
      100 / 0.25 / 830 / 1 / @NQ#C dictionary), and the engine's $/point really
      equals the config's bigpointvalue (not a constant).

Exit 0 only if the config is actually wired through.
"""
import os
import sys
from dataclasses import replace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.run_gt import FillEngine
from pl_transpiler.runtime.instrument_config import InstrumentConfig, NQ, get_config


def _run_round_trip(config):
    """Drive one FillEngine through a fixed long entry+exit and return net_profit.

    Sequence (identical regardless of config):
      bar 1: queue 'buy 1 @ market next bar'
      bar 2: fill the buy at Open=100.0; queue 'sell 1 @ market next bar'
      bar 3: fill the sell at Open=110.0  -> realized +10 points * 1 contract

    Realized PnL = (110 - 100) * bigpointvalue * 1 = 10 * bigpointvalue.
    """
    fe = FillEngine(config=config)
    # bar 1: strategy emits a long entry; nothing to fill yet.
    fe.before_bar(open_price=100.0, close_price=100.0, high=100.0, low=100.0,
                  barnumber=1)
    fe.after_bar([('buy', 'market', 1, 'next', 'LE')], {}, close_price=100.0)
    # bar 2: pending buy fills at Open=100.0; strategy emits the exit.
    fe.before_bar(open_price=100.0, close_price=105.0, high=106.0, low=99.0,
                  barnumber=2)
    fe.after_bar([('sell', 'market', 1, 'next', 'LX')], {}, close_price=105.0)
    # bar 3: pending sell fills at Open=110.0 -> position closes.
    fe.before_bar(open_price=110.0, close_price=110.0, high=110.0, low=110.0,
                  barnumber=3)
    fe.after_bar([], {}, close_price=110.0)
    return fe


def test_nq_config_values():
    """The NQ config reproduces today's exact validated chart/instrument values."""
    cfg = get_config('NQ')
    assert cfg is NQ
    assert cfg.bigpointvalue == 20
    assert cfg.pointvalue == 0.20
    assert cfg.minmove == 25
    assert cfg.pricescale == 100
    assert cfg.tick == 0.25
    assert cfg.sess1starttime == 830
    assert cfg.barinterval == 1
    assert cfg.symccy == 1
    assert cfg.symnm == '@NQ#C'
    assert cfg.symalt == '@NQ#C'
    assert cfg.descstr == 'E-MINI NASDAQ 100'
    assert cfg.expdate == 1130331
    print("  [ok] NQ config reproduces today's exact validated values")


def test_engine_uses_config_point_value():
    """FillEngine's $/point is the config's bigpointvalue, not a constant."""
    nq = FillEngine(config=NQ)
    synth = FillEngine(config=replace(NQ, name='SYN', bigpointvalue=7))
    assert nq.point_value == 20, f"NQ engine point_value={nq.point_value}, want 20"
    assert synth.point_value == 7, (
        f"synthetic engine point_value={synth.point_value}, want 7 — "
        "config bigpointvalue is NOT wired into the engine")
    print("  [ok] engine $/point follows injected config (20 vs 7)")


def test_fill_pnl_responds_to_config():
    """Realized fill PnL scales with the injected bigpointvalue (not hard-coded 20)."""
    # Synthetic config: same NQ chart but a DIFFERENT big-point-value.
    synth = replace(NQ, name='SYN', bigpointvalue=7)
    assert synth.bigpointvalue != NQ.bigpointvalue

    nq_pnl = _run_round_trip(NQ).net_profit
    syn_pnl = _run_round_trip(synth).net_profit

    # 10-point round trip * 1 contract.
    assert nq_pnl == 10 * NQ.bigpointvalue == 200.0, (
        f"NQ net_profit={nq_pnl}, want 200.0 (10pts * bigpointvalue 20)")
    assert syn_pnl == 10 * synth.bigpointvalue == 70.0, (
        f"synthetic net_profit={syn_pnl}, want 70.0 (10pts * bigpointvalue 7) — "
        "fill PnL did NOT respond to the injected config")
    # The ratio must be exactly the bigpointvalue ratio: proves no hard-coded 20.
    assert nq_pnl / syn_pnl == NQ.bigpointvalue / synth.bigpointvalue, (
        f"PnL ratio {nq_pnl/syn_pnl} != config ratio "
        f"{NQ.bigpointvalue/synth.bigpointvalue}")
    print(f"  [ok] fill PnL responds to config: NQ={nq_pnl} synth={syn_pnl} "
          f"(ratio {nq_pnl/syn_pnl:.4g} == bigpointvalue ratio)")


def main():
    tests = [
        test_nq_config_values,
        test_engine_uses_config_point_value,
        test_fill_pnl_responds_to_config,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL RUNTIME-CONFIG TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
