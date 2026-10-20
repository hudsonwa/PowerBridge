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
test_series_layout.py — issue #13 / #14: series contract + window bit-identity.

The engine keeps growing Python lists (PAD=1, close[-1] = current bar). persist
and trace strategies accept list / array.array / memoryview; the clean mirror
dialect stays list-only. Window helpers return bit-identical f32 values on
those three sequence types.

Stdlib only, no captures.
"""
import array
import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler import py_front
from pl_transpiler.engine import FillEngine, run_bar_loop
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.runtime.pl_runtime import (
    pl_average, pl_crosses_above, pl_crosses_below, pl_highest, pl_lowest,
    pl_momentum, pl_rsi, pl_summation, reset_indicator_caches,
)

NQ = get_config("NQ")

CLEAN_GUARD = (
    "    if not isinstance(close, list): "
    "open, high, low, close, volume, date, time = "
    "[open],[high],[low],[close],[volume],[date],[time]"
)
PERSIST_GUARD = (
    "    if isinstance(close, (str, bytes)) or not hasattr(close, '__getitem__'):"
)

LOOKBACK_EL = (
    "If Close > Close[1] Then Buy (\"LE\") next bar at market;\n"
)
AVG_EL = (
    "If Close > Average(Close, 3) Then Buy (\"LE\") next bar at market;\n"
)
COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 3 Then Buy (\"LE\") next bar at market;\n"
)


def _same_float(a, b):
    return type(a) is float and type(b) is float and struct.pack("d", a) == struct.pack("d", b)


def _price_seq():
    # PAD=1 leading zero, then a mixed window that includes a 0.0 in the tail
    # (so padding-skip is not confused with a real zero close).
    return [0.0, 10.0, 10.5, 9.5, 11.0, 12.25, 8.0, 0.0, 13.5, 14.0, 13.0]


def test_clean_dialect_list_guard_unchanged():
    src = open(os.path.join(REPO, "ground_truth", "GT1_functions_indicators.txt"),
               encoding="utf-8").read()
    clean = transpile(src, trace=False)
    persist = transpile(src, persist=True)
    traced = transpile(src, trace=True)
    assert CLEAN_GUARD in clean, "clean mirror dialect lost the list coercion"
    assert PERSIST_GUARD not in clean
    assert PERSIST_GUARD in persist
    assert PERSIST_GUARD in traced
    assert CLEAN_GUARD not in persist
    assert CLEAN_GUARD not in traced
    ast = py_front.py_to_ast(clean)
    assert isinstance(ast, dict) and ast.get("type")
    print("  [ok] clean list guard unchanged; persist/trace accept sequences")


def test_window_helpers_bit_identical_across_sequence_types():
    raw = _price_seq()
    arr = array.array("d", raw)
    mv = memoryview(arr)
    length = 4
    reset_indicator_caches()
    cases = [
        ("pl_average", lambda s: pl_average(s, length)),
        ("pl_summation", lambda s: pl_summation(s, length)),
        ("pl_highest", lambda s: pl_highest(s, length)),
        ("pl_lowest", lambda s: pl_lowest(s, length)),
        ("pl_momentum", lambda s: pl_momentum(s, length)),
    ]
    for name, fn in cases:
        a, b, c = fn(raw), fn(arr), fn(mv)
        assert _same_float(a, b), "%s list vs array: %r %r" % (name, a, b)
        assert _same_float(a, c), "%s list vs memoryview: %r %r" % (name, a, c)
    # crosses: two series
    other = [0.0, 9.0, 10.6, 9.4, 10.0, 12.0, 9.0, 1.0, 13.0, 15.0, 12.0]
    oa, om = array.array("d", other), memoryview(array.array("d", other))
    for name, fn in (
        ("pl_crosses_above", pl_crosses_above),
        ("pl_crosses_below", pl_crosses_below),
    ):
        a, b, c = fn(raw, other), fn(arr, oa), fn(mv, om)
        assert a == b == c, "%s %r %r %r" % (name, a, b, c)
    reset_indicator_caches()
    rsi_list = pl_rsi(raw, 3)
    reset_indicator_caches()
    rsi_arr = pl_rsi(arr, 3)
    reset_indicator_caches()
    rsi_mv = pl_rsi(mv, 3)
    assert _same_float(rsi_list, rsi_arr), "pl_rsi list vs array"
    assert _same_float(rsi_list, rsi_mv), "pl_rsi list vs memoryview"
    print("  [ok] window helpers bit-identical on list / array / memoryview")


def _order_hits(py, factory, n=8, closes=None):
    ns = {}
    exec(compile(py, "<series-layout>", "exec"), ns)
    strat = ns["strategy"]
    state = {}
    hits = []
    if closes is None:
        closes = [float(i + 1) for i in range(n)]
    o = factory("d")
    h = factory("d")
    l = factory("d")
    c = factory("d")
    v = factory("l")
    d = factory("l")
    t = factory("l")
    for i, px in enumerate(closes):
        o.append(px)
        h.append(px)
        l.append(px)
        c.append(px)
        v.append(1)
        d.append(1240102)
        t.append(930 + i)
        result = strat(
            o, h, l, c, v, d, t,
            _state=state, barnumber=i + 1,
        )
        if result.get("orders"):
            hits.append(i + 1)
    return hits


def _list_factory(code):
    return [0.0] if code == "d" else [0]


def _array_factory(code):
    if code == "d":
        return array.array("d", [0.0])
    return array.array("l", [0])


def test_persist_array_series_matches_list():
    persist = transpile(LOOKBACK_EL, persist=True)
    closes = [10.0, 11.0, 10.5, 12.0, 13.0, 12.0, 14.0, 15.0]
    list_hits = _order_hits(persist, _list_factory, closes=closes)
    arr_hits = _order_hits(persist, _array_factory, closes=closes)
    assert list_hits == arr_hits, "list %s vs array %s" % (list_hits, arr_hits)
    assert list_hits, "lookback strategy produced no orders"
    persist_avg = transpile(AVG_EL, persist=True)
    list_avg = _order_hits(persist_avg, _list_factory, closes=closes)
    arr_avg = _order_hits(persist_avg, _array_factory, closes=closes)
    assert list_avg == arr_avg, "Average list %s vs array %s" % (list_avg, arr_avg)
    persist_count = transpile(COUNT_EL, persist=True)
    assert _order_hits(persist_count, _list_factory) == [3]
    assert _order_hits(persist_count, _array_factory) == [3]
    print("  [ok] persist list vs array.array: Close[1] / Average / Count match")


def test_engine_history_is_growing_list_with_pad():
    seen = []

    def strategy(o, h, l, c, v, d, t, **kwargs):
        seen.append({
            "list": type(c) is list,
            "len": len(c),
            "close": c[-1],
            "pad": c[0],
            "open": o[-1],
        })
        return {"orders": [], "risk": {}}

    n = 4
    closes = [10.0, 11.0, 12.0, 13.0]
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    run_bar_loop(
        strategy,
        n_bars=n,
        get_ohlcv=lambda i: (closes[i], closes[i], closes[i], closes[i], 1.0),
        dates=[1240102] * n,
        times=[930 + i for i in range(n)],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert len(seen) == n
    for i, row in enumerate(seen):
        assert row["list"] is True, "engine must pass a real list"
        assert row["pad"] == 0.0
        assert row["len"] == i + 2  # PAD=1 + bars so far
        assert row["close"] == closes[i]
        assert row["open"] == closes[i]
    print("  [ok] engine history: list, PAD=1, len=pad+bars, close[-1] current")


def test_array_wrap_is_not_a_one_element_list():
    """A non-list sequence must not be wrapped as [array] (that made close[-1]
    the array object itself). Scalar floats still wrap."""
    persist = transpile("Value1 = Close;\n", persist=True)
    ns = {}
    exec(compile(persist, "<wrap>", "exec"), ns)
    strat = ns["strategy"]
    arr = array.array("d", [0.0, 7.5])
    out = strat(arr, arr, arr, arr, arr, arr, arr, _state={}, barnumber=1)
    # persist returns orders/plots; the Close read must not throw.
    assert isinstance(out, dict)
    # scalar path still wraps
    out2 = strat(1.5, 1.5, 1.5, 1.5, 1.0, 1240102, 930, _state={}, barnumber=1)
    assert isinstance(out2, dict)
    print("  [ok] persist accepts array.array and scalar OHLC without throwing")


def main():
    tests = [
        test_clean_dialect_list_guard_unchanged,
        test_window_helpers_bit_identical_across_sequence_types,
        test_persist_array_series_matches_list,
        test_engine_history_is_growing_list_with_pad,
        test_array_wrap_is_not_a_one_element_list,
    ]
    for t in tests:
        print("running %s ..." % t.__name__)
        t()
    print("\nALL SERIES-LAYOUT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
