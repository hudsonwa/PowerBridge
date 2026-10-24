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
"""Strict mode and the unimplemented-call counts are per run (issue #244)."""
import os
import sys
import threading
from typing import Any

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.codegen_el import emit_el  # noqa: E402
from pl_transpiler.lexer import tokenise  # noqa: E402
from pl_transpiler.parser import parse, parse_source  # noqa: E402
from pl_transpiler.runtime import _rt_core  # noqa: E402
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))[:20]
SRC = "Vars: x(0);\nArrays: a[5](0);\nx = ArraySize(a);\n"


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_rt_core._DEFAULT_STATE, "strict", True)
    monkeypatch.setattr(_rt_core._DEFAULT_STATE, "unimpl_calls", {})


def _lenient_run() -> dict[str, Any]:
    with _rt_core._run_state(strict=False):
        return run_el(SRC, BARS, NQ)


def test_runs_with_different_strictness_do_not_interfere(strict: None) -> None:
    out = _lenient_run()
    assert out["unimplemented"] == {"arraysize": len(BARS)}
    # The lenient run left the process default strict and its counts empty.
    with pytest.raises(UnimplementedBuiltinError):
        run_el(SRC, BARS, NQ)
    assert _rt_core._DEFAULT_STATE.strict is True
    assert _rt_core._DEFAULT_STATE.unimpl_calls == {}
    # A second lenient run starts its counts from zero.
    assert _lenient_run()["unimplemented"] == {"arraysize": len(BARS)}


def test_concurrent_runs_keep_their_own_strictness(strict: None) -> None:
    results: dict[str, Any] = {}
    start = threading.Barrier(4)

    def lenient(key: str) -> None:
        start.wait()
        results[key] = _lenient_run()["unimplemented"]

    def strict_run(key: str) -> None:
        start.wait()
        try:
            run_el(SRC, BARS, NQ)
            results[key] = "no error"
        except UnimplementedBuiltinError as exc:
            results[key] = exc.builtin

    threads = [threading.Thread(target=lenient, args=("l1",)),
               threading.Thread(target=strict_run, args=("s1",)),
               threading.Thread(target=lenient, args=("l2",)),
               threading.Thread(target=strict_run, args=("s2",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"l1": {"arraysize": len(BARS)}, "l2": {"arraysize": len(BARS)},
                       "s1": "arraysize", "s2": "arraysize"}
    assert _rt_core._DEFAULT_STATE.unimpl_calls == {}


def test_set_strict_mode_inside_a_run_stays_in_that_run(strict: None) -> None:
    with _rt_core._run_state():
        _rt_core.set_strict_mode(False)
        assert _rt_core._unimplemented("pl_arraysize") == 0
    assert _rt_core._DEFAULT_STATE.strict is True


def _emit(src: str) -> str:
    tokens, positions = tokenise(src)
    return emit_el(parse(tokens, positions=positions, source=src))


def test_concurrent_el_emits_keep_their_own_comments() -> None:
    # Issue #256: the lexer used to hand comments to the parser through a
    # module-level list, so concurrent parses could swap comments. More sources
    # than the parse cache holds also exercise its eviction under threads.
    srcs = [f"Vars: x(0); {{c{i}}}\nx = {i}; // note {i}\n" for i in range(12)]
    want = [_emit(s) for s in srcs]
    got: dict[int, list[str]] = {}
    errors: list[BaseException] = []

    def emit(i: int) -> None:
        try:
            out = []
            for k in range(200):
                out.append(_emit(srcs[i]))
                j = (i + k) % len(srcs)
                if emit_el(parse_source(srcs[j])) != want[j]:
                    out.append(f"cached mismatch for {j}")
            got[i] = out
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads often so a race shows up
    try:
        threads = [threading.Thread(target=emit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old_interval)
    assert errors == []
    for i in range(8):
        assert f"note {i}" in want[i]
        assert got[i] == [want[i]] * 200
