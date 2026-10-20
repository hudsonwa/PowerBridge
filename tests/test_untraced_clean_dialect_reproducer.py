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

"""Documented reproducer: running the clean mirror dialect as a strategy engine input.

This lane's diagnosis is that the clean (trace=False, persist=False) dialect is being
executed in a bar loop where state persistence is required. That dialect intentionally
does not persist EL Variables across bars, so a state-gated order system can silently
emit zero trades.
"""

import pytest

from pl_transpiler import transpile
from pl_transpiler.engine import FillEngine, run_bar_loop
from pl_transpiler.runtime.instrument_config import get_config


_STATEFUL_GATE_EL = """
Variables: gate(0);

gate = gate + 1;

If gate = 2 Then
    Buy ("LE") next bar at market;

If gate = 4 Then
    Sell ("LX") next bar at market;
"""


def _fills_for_transpile(*, trace=False, persist=None):
    py = transpile(_STATEFUL_GATE_EL, trace=trace, persist=persist)
    ns = {}
    exec(compile(py, "<repro>", "exec"), ns)
    strategy = ns["strategy"]

    bars = [
        (1.0, 1.2, 0.8, 1.0, 1.0),
        (1.0, 1.2, 0.8, 1.0, 1.0),
        (1.0, 1.2, 0.8, 1.0, 1.0),
        (1.0, 1.2, 0.8, 1.0, 1.0),
        (1.0, 1.2, 0.8, 1.0, 1.0),
        (1.0, 1.2, 0.8, 1.0, 1.0),
    ]
    dates = [1240102] * len(bars)
    times = [930 + i for i in range(len(bars))]

    fe = FillEngine(config=get_config("NQ"))
    fe._emit_gta6 = False
    run_bar_loop(
        strategy,
        n_bars=len(bars),
        get_ohlcv=lambda i: bars[i],
        dates=dates,
        times=times,
        config=get_config("NQ"),
        fill_engine=fe,
        emit_row=lambda *_: None,
        strict=True,
        emit_trace=False,
    )
    return len(fe.get_mc_trades())


@pytest.mark.xfail(reason="Documented reproducer: clean mirror dialect run as strategy input")
def test_clean_untraced_mirror_dialect_silent_zero_trade_reproducer():
    traced_fills = _fills_for_transpile(trace=True)
    clean_fills = _fills_for_transpile(trace=False)
    assert traced_fills == clean_fills
