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
test_persist_mode.py — runnable no-trace dialect.

transpile(dialect="mirror") is the clean mirror dialect for py_front and does NOT
persist EL Variables across bars (every var is re-initialised). That is
intentional and must stay byte-identical.

transpile(persist=True) (trace defaults False) emits the same `_state`
container the engine already threads, without the per-bar `_trace[...]`
stores. That is the runnable no-trace dialect.

transpile(trace=True) is unchanged: persist defaults to matching trace, so
trace-mode output stays byte-identical to persist=True, trace=True.

Exit 0 iff all groups pass. Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import py_front, transpile

GT1 = os.path.join(REPO, "ground_truth", "GT1_functions_indicators.txt")

# Cross-bar counter: only fires an order on bar 3 if Count survives from bar
# to bar. Clean dialect resets Count every bar, so the Buy is unreachable.
COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 3 Then Buy (\"LE\") next bar at market;\n"
)


def _order_bars(py, n=8):
    """Exec generated strategy over n synthetic bars; return bars that emitted orders."""
    ns = {}
    exec(compile(py, "<persist-test>", "exec"), ns)
    strat = ns["strategy"]
    state = {}
    hits = []
    series = [0.0, 1.0]
    for i in range(n):
        result = strat(
            series, series, series, series, series, series, series,
            _state=state, barnumber=i + 1,
        )
        if result.get("orders"):
            hits.append(i + 1)
    return hits, result


def test_clean_dialect_unchanged():
    src = open(GT1, encoding="utf-8").read()
    clean = transpile(src, dialect="mirror")
    explicit = transpile(src, trace=False, persist=False)
    assert clean == explicit, "clean dialect drifted"
    assert transpile(src) == transpile(src, trace=False, persist=True), \
        "library default must be the runnable dialect"
    assert "_state = kwargs.setdefault" not in clean
    assert "_trace =" not in clean
    # py_front still reads the clean mirror.
    ast = py_front.py_to_ast(clean)
    assert isinstance(ast, dict) and ast.get("type")
    print("  [ok] clean dialect byte-identical; py_front accepts it")


def test_trace_mode_unchanged():
    src = open(GT1, encoding="utf-8").read()
    traced = transpile(src, trace=True)
    explicit = transpile(src, trace=True, persist=True)
    assert traced == explicit, "trace=True output drifted when persist follows"
    assert "_state = kwargs.setdefault" in traced
    assert "_trace =" in traced
    print("  [ok] trace=True byte-identical to persist=True, trace=True")


def test_persist_only_has_state_not_trace():
    src = open(GT1, encoding="utf-8").read()
    persist = transpile(src, persist=True)
    traced = transpile(src, trace=True)
    clean = transpile(src, dialect="mirror")
    assert persist != clean, "persist-only must differ from the clean dialect"
    assert persist != traced, "persist-only must omit _trace vs trace=True"
    assert "_state = kwargs.setdefault" in persist
    assert "_trace =" not in persist
    assert "_trace[" not in persist
    assert "'_trace': _trace" not in persist
    # GT1's AvgC is passed to CrossesAbove, so it keeps a lookback list.
    assert "_state['avgc'].append(avgc)" in persist
    print("  [ok] persist=True emits _state and omits _trace")


def test_count_strategy_needs_persist():
    clean = transpile(COUNT_EL, dialect="mirror")
    persist = transpile(COUNT_EL, persist=True)
    traced = transpile(COUNT_EL, trace=True)

    clean_hits, clean_ret = _order_bars(clean)
    persist_hits, persist_ret = _order_bars(persist)
    traced_hits, traced_ret = _order_bars(traced)

    assert clean_hits == [], (
        f"clean dialect should not arm a cross-bar counter, got orders on {clean_hits}")
    assert persist_hits == [3], (
        f"persist-only should fire on bar 3, got {persist_hits}")
    assert traced_hits == [3], (
        f"trace=True should still fire on bar 3, got {traced_hits}")
    assert "_trace" not in persist_ret
    assert "_trace" in traced_ret
    print("  [ok] Count=3 Buy: clean=[], persist=[3], trace=[3]")


def test_persist_cli_flag_shape():
    # The generated persist-only Count program is a valid exec module and
    # does not mention _trace even as a return key.
    persist = transpile(COUNT_EL, persist=True)
    assert "def strategy" in persist
    assert persist.count("_trace") == 0
    print("  [ok] persist-only Count program has zero _trace tokens")


def test_persist_first_bar_state_seed():
    """Persist/trace seed `_state` once; they must not allocate `[init]`
    via setdefault on every bar. Clean dialect is unchanged (no `_state`)."""
    persist = transpile(COUNT_EL, persist=True)
    traced = transpile(COUNT_EL, trace=True)
    clean = transpile(COUNT_EL, dialect="mirror")
    for label, py in (("persist", persist), ("trace", traced)):
        assert "if '__pb_seeded' not in _state:" in py, label
        assert "_state['__pb_seeded'] = 1" in py, label
        assert "_state.setdefault('" not in py, (
            f"{label} still emits per-bar _state.setdefault: {py}")
        assert "_state['__pb_slots'] = [0]" in py, label
        assert "count, = _pb_slots" in py, label
        assert "_pb_slots[:] = (count,)" in py, label
        assert "_state['count'] = 0" not in py, label
        assert "_state['count'] = count" not in py, label
        assert "_state['count'].append(" not in py, label
    assert "if '__pb_seeded' not in _state:" not in clean
    assert "_state['__pb_seeded']" not in clean
    print("  [ok] persist/trace first-bar seed; no per-bar _state.setdefault")


INPUT_EL = (
    "Inputs: Threshold(3);\n"
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = Threshold Then Buy (\"LE\") next bar at market;\n"
)


def test_persist_hoists_inputs():
    persist = transpile(INPUT_EL, persist=True)
    clean = transpile(INPUT_EL, dialect="mirror")
    assert "_state['__pb_inputs']" in persist
    assert "threshold, = _state['__pb_inputs']" in persist
    assert "if '__pb_seeded' not in _state:" in persist
    # First-bar seed still *captures* an override via kwargs.get; after that
    # the Input is the unpacked tuple, not a per-bar kwargs.get.
    assert persist.count("kwargs.get('threshold', 3)") == 1
    assert "kwargs.get('threshold', 3)" in clean
    assert "_state['__pb_inputs']" not in clean
    print("  [ok] persist hoists Inputs to __pb_inputs; clean keeps kwargs.get")


def test_input_threshold_strategy():
    persist = transpile(INPUT_EL, persist=True)
    clean = transpile(INPUT_EL, dialect="mirror")
    traced = transpile(INPUT_EL, trace=True)

    def _hits(py, n=8, **extra):
        ns = {}
        exec(compile(py, "<input-test>", "exec"), ns)
        strat = ns["strategy"]
        state = {}
        hits = []
        series = [0.0, 1.0]
        for i in range(n):
            result = strat(
                series, series, series, series, series, series, series,
                _state=state, barnumber=i + 1, **extra,
            )
            if result.get("orders"):
                hits.append(i + 1)
        return hits

    assert _hits(clean) == [], "clean dialect must not arm Count"
    assert _hits(persist) == [3], f"persist default Threshold=3, got {_hits(persist)}"
    assert _hits(traced) == [3]
    assert _hits(persist, threshold=5) == [5], "first-bar Input override must apply"
    print("  [ok] Threshold Input: clean=[], persist/trace=[3], override=[5]")


LOOKBACK_EL = (
    "Vars: Count(0), Prev(0);\n"
    "Count = Count + 1;\n"
    "Prev = Count[1];\n"
    "If Prev = 2 Then Buy (\"LE\") next bar at market;\n"
)

CROSSES_EL = (
    "Vars: AvgC(0);\n"
    "AvgC = Average(Close, 3);\n"
    "If Close crosses above AvgC Then Buy (\"LE\") next bar at market;\n"
)


def test_unindexed_var_is_scalar():
    persist = transpile(COUNT_EL, persist=True)
    traced = transpile(COUNT_EL, trace=True)
    for label, py in (("persist", persist), ("trace", traced)):
        assert "count, = _pb_slots" in py, label
        assert "count = _state['count']" not in py, label
        assert "count = _state['count'][-1]" not in py, label
        assert "_pb_slots[:] = (count,)" in py, label
        assert "_state['count'] = count" not in py, label
        assert "].append(" not in py, label
    clean = transpile(COUNT_EL, dialect="mirror")
    assert "_state" not in clean
    print("  [ok] unindexed Count is a persist scalar packed in __pb_slots; clean has no _state")


TWO_SCALAR_EL = (
    "Vars: A(0), B(0);\n"
    "A = A + 1;\n"
    "B = A;\n"
    "If B = 3 Then Buy (\"LE\") next bar at market;\n"
)


def test_two_scalars_unpack_as_tuple():
    persist = transpile(TWO_SCALAR_EL, persist=True)
    assert "_state['__pb_slots'] =" in persist
    assert "a, b = _pb_slots" in persist
    assert "_pb_slots[:] = (a, b)" in persist
    assert "_state['a'] = a" not in persist
    assert "_state['b'] = b" not in persist
    hits, _ = _order_bars(persist)
    assert hits == [3], f"two-scalar persist should fire on bar 3, got {hits}"
    print("  [ok] two unindexed vars unpack/store as a slots tuple; fires on bar 3")


def test_indexed_var_keeps_history():
    persist = transpile(LOOKBACK_EL, persist=True)
    traced = transpile(LOOKBACK_EL, trace=True)
    for label, py in (("persist", persist), ("trace", traced)):
        assert "_state['count'] = [0]" in py, label
        assert "count = _state['count'][-1]" in py, label
        assert "_state['count'].append(count)" in py, label
        # Prev is only assigned, never indexed — packed scalar.
        assert "prev, = _pb_slots" in py, label
        assert "_pb_slots[:] = (prev,)" in py, label
        assert "prev = _state['prev']" not in py, label
        assert "_state['prev'] = prev" not in py, label
        assert "_state['prev'].append(" not in py, label
    persist_hits, _ = _order_bars(persist)
    traced_hits, _ = _order_bars(traced)
    assert persist_hits == [3], f"Count[1]==2 should fire on bar 3, got {persist_hits}"
    assert traced_hits == [3], f"trace Count[1] should fire on bar 3, got {traced_hits}"
    print("  [ok] Count[1] keeps a history list; Prev stays a scalar; fires on bar 3")


def test_series_func_arg_keeps_history():
    persist = transpile(CROSSES_EL, persist=True)
    assert "_state['avgc'] = [f32(0)]" in persist or "_state['avgc'] = [0]" in persist
    assert "(_state['avgc'] + [avgc])" in persist
    assert "_state['avgc'].append(avgc)" in persist
    clean = transpile(CROSSES_EL, dialect="mirror")
    assert "_state = kwargs.setdefault" not in clean
    print("  [ok] CrossesAbove(Close, AvgC) keeps AvgC as a history list")


OHLCV_EL = (
    "Vars: Px(0);\n"
    "Px = Close;\n"
    "If Px > Open Then Buy (\"LE\") next bar at market;\n"
)


def test_persist_hoists_ohlcv_f32():
    """Persist/trace f32 current-bar OHLCV once; clean keeps f32(f32(close[-1]))."""
    persist = transpile(OHLCV_EL, persist=True)
    traced = transpile(OHLCV_EL, trace=True)
    clean = transpile(OHLCV_EL, dialect="mirror")
    for label, py in (("persist", persist), ("trace", traced)):
        for line in (
            "_open = f32(open[-1])",
            "_high = f32(high[-1])",
            "_low = f32(low[-1])",
            "_close = f32(close[-1])",
            "_volume = f32(volume[-1])",
        ):
            assert line in py, f"{label} missing hoist {line}"
        assert "px = _close" in py, f"{label} assign Close should be px = _close:\n{py}"
        assert "f32(_close)" not in py, f"{label} still double-wraps _close:\n{py}"
        assert "f32(f32(" not in py, f"{label} still emits f32(f32(...)):\n{py}"
        assert py.count("f32(close[-1])") == 1, f"{label} extra close wraps:\n{py}"
        assert py.count("f32(open[-1])") == 1, f"{label} extra open wraps:\n{py}"
        assert "px > _open" in py, f"{label} compare should use hoisted Open:\n{py}"
    assert "_open = f32(open[-1])" not in clean
    assert "px = f32(f32(close[-1]))" in clean, f"clean assign drifted:\n{clean}"
    assert "f32(open[-1])" in clean, f"clean Open wrap drifted:\n{clean}"
    print("  [ok] persist/trace hoist OHLCV f32 once; clean keeps double wrap")


def test_persist_ohlcv_assign_is_bit_identical_to_double_wrap():
    """Hoisted `_close` equals f32(f32(close[-1])) on a non-f32 input."""
    from pl_transpiler.runtime.pl_runtime import f32
    persist = transpile(OHLCV_EL, persist=True)
    ns = {}
    exec(compile(persist, "<ohlcv-test>", "exec"), ns)
    strat = ns["strategy"]
    # 0.1 is not a float32 value; a missed wrap would disagree with EL.
    assert f32(f32(0.1)) == f32(0.1)
    opens = [0.1]
    closes = [0.2]
    result = strat(
        opens, closes, opens, closes, opens, [1010101], [930],
        _state={}, barnumber=1,
    )
    assert result.get("orders"), "Px=Close=0.2 > Open=0.1 should Buy"
    # Equal prices must not fire (Px > Open).
    eq = [0.1]
    quiet = strat(
        eq, eq, eq, eq, eq, [1010101], [930],
        _state={}, barnumber=1,
    )
    assert not quiet.get("orders"), "Px == Open must not Buy"
    print("  [ok] persist Close/Open hoist fires Buy iff Close > Open")


def main():
    tests = [
        test_clean_dialect_unchanged,
        test_trace_mode_unchanged,
        test_persist_only_has_state_not_trace,
        test_count_strategy_needs_persist,
        test_persist_cli_flag_shape,
        test_persist_first_bar_state_seed,
        test_persist_hoists_inputs,
        test_input_threshold_strategy,
        test_unindexed_var_is_scalar,
        test_two_scalars_unpack_as_tuple,
        test_indexed_var_keeps_history,
        test_series_func_arg_keeps_history,
        test_persist_hoists_ohlcv_f32,
        test_persist_ohlcv_assign_is_bit_identical_to_double_wrap,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL PERSIST-MODE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
