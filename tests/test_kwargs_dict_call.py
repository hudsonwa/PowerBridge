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
test_kwargs_dict_call.py — persist/trace strategy() accepts the kwargs dict
by reference so run_bar_loop can skip a per-bar **unpack copy.

Clean (mirror) dialect stays `def strategy(..., **kwargs)` for py_front.
Direct persist callers that pass `_state=` / `barnumber=` as keywords still
work via the `kwargs=None, **_kw` fallback.

Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler import py_front
from pl_transpiler.engine import FillEngine, run_bar_loop
from pl_transpiler.runtime.instrument_config import get_config

NQ = get_config("NQ")

COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 3 Then Buy (\"LE\") next bar at market;\n"
)


def test_clean_dialect_keeps_starstar_kwargs():
    clean = transpile(COUNT_EL, trace=False)
    assert "def strategy(open, high, low, close, volume, date, time, **kwargs):" in clean
    assert "kwargs=None" not in clean
    ast = py_front.py_to_ast(clean)
    assert isinstance(ast, dict) and ast.get("type")
    print("  [ok] clean dialect still **kwargs; py_front accepts it")


def test_persist_and_trace_take_kwargs_positional():
    persist = transpile(COUNT_EL, persist=True)
    traced = transpile(COUNT_EL, trace=True)
    needle = "def strategy(open, high, low, close, volume, date, time, kwargs=None, **_kw):"
    for label, py in (("persist", persist), ("trace", traced)):
        assert needle in py, label
        assert "if kwargs is None:" in py, label
        assert "kwargs = _kw" in py, label
    print("  [ok] persist/trace take kwargs as 8th positional with **_kw fallback")


def test_run_bar_loop_passes_same_dict():
    ids = []

    def strategy(open, high, low, close, volume, date, time, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        ids.append(id(kwargs))
        return {"orders": [], "risk": {}}

    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    run_bar_loop(
        strategy,
        n_bars=5,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102] * 5,
        times=[930 + i for i in range(5)],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert len(ids) == 5, ids
    assert len(set(ids)) == 1, f"expected one reused dict, got {len(set(ids))} ids"
    print("  [ok] run_bar_loop reuses one kwargs dict for pass-dict strategies")


def test_starstar_strategies_still_run():
    """Hand-written **kwargs strategies keep working (copied unpack)."""
    seen = []

    def strategy(*_series, **kwargs):
        seen.append(dict(kwargs))
        return {"orders": [], "risk": {}}

    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    run_bar_loop(
        strategy,
        n_bars=2,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102, 1240102],
        times=[930, 931],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert len(seen) == 2
    assert seen[0]["barnumber"] == 1
    assert seen[1]["barnumber"] == 2
    print("  [ok] hand-written **kwargs strategy still runs via unpack path")


def test_direct_keyword_call_still_persists_count():
    """test_persist_mode style: strategy(*series, _state=..., barnumber=...)."""
    py = transpile(COUNT_EL, persist=True)
    ns = {}
    exec(compile(py, "<kwdict-direct>", "exec"), ns)
    strat = ns["strategy"]
    state = {}
    series = [0.0, 1.0]
    hits = []
    for i in range(8):
        result = strat(
            series, series, series, series, series, series, series,
            _state=state, barnumber=i + 1,
        )
        if result.get("orders"):
            hits.append(i + 1)
    assert hits == [3], hits
    print("  [ok] direct _state=/barnumber= keyword call still fires on bar 3")


def test_persist_via_run_bar_loop_fires_on_bar_3():
    py = transpile(COUNT_EL, persist=True)
    ns = {}
    exec(compile(py, "<kwdict-loop>", "exec"), ns)
    strat = ns["strategy"]
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    n = 8
    run_bar_loop(
        strat,
        n_bars=n,
        get_ohlcv=lambda i: (1.0, 1.1, 0.9, 1.05, 1.0),
        dates=[1240102] * n,
        times=[930 + i for i in range(n)],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    # Buy next bar at market on bar 3 -> fill on bar 4. The position is still
    # open, so total_trades (closed) stays 0; the fill list records the entry.
    assert len(fe.fills) == 1, fe.fills
    assert fe.market_position == 1 and fe.contracts == 1
    print("  [ok] persist COUNT_EL via run_bar_loop fills after bar-3 Buy")


def main():
    tests = [
        test_clean_dialect_keeps_starstar_kwargs,
        test_persist_and_trace_take_kwargs_positional,
        test_run_bar_loop_passes_same_dict,
        test_starstar_strategies_still_run,
        test_direct_keyword_call_still_persists_count,
        test_persist_via_run_bar_loop_fires_on_bar_3,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL KWARGS-DICT-CALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
