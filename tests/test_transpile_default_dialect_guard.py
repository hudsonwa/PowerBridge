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

"""Guard: the library default of ``transpile`` is the runnable dialect.

The clean mirror dialect (``dialect="mirror"``) does not persist EL Variables
across bars, so a state-gated order system run from it used to emit zero trades
silently. The default is now runnable, and the bar loop refuses mirror output.
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


_BARS = [(1.0, 1.2, 0.8, 1.0, 1.0)] * 6


def _fills_for_transpile(trades=False, **kw):
    py = transpile(_STATEFUL_GATE_EL, **kw)
    ns = {}
    exec(compile(py, "<repro>", "exec"), ns)
    strategy = ns["strategy"]

    bars = _BARS
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
    return fe.get_mc_trades() if trades else len(fe.get_mc_trades())


def test_default_transpile_matches_traced_fills():
    traced_fills = _fills_for_transpile(trace=True)
    default_fills = _fills_for_transpile()
    assert traced_fills == default_fills > 0


def test_mirror_dialect_refused_by_bar_loop():
    with pytest.raises(ValueError, match="mirror-dialect"):
        _fills_for_transpile(dialect="mirror")


def test_stateful_default_matches_run_el():
    from pl_transpiler.tools.pl_run import run_el
    bars = [dict(date=1240102, time=930 + i, open=1.0, high=1.2, low=0.8,
                 close=1.0, volume=1.0) for i in range(len(_BARS))]
    ref = run_el(_STATEFUL_GATE_EL, bars, get_config("NQ"))["trades"]
    assert ref and _fills_for_transpile(trades=True) == ref
