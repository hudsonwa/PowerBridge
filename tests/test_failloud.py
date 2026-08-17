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
test_failloud.py — regression test for the FAIL-LOUD runner (GOAL unit #1).

Asserts that tests/run_gt.py no longer silently swallows per-bar strategy
exceptions:

  (a) An always-throwing strategy is DETECTED: the throw sidecar reports
      throw_count == total_bars, and PL_STRICT=1 makes the run re-raise.
  (b) A real GT run (GT1) throws on ZERO bars.

Exit 0 only if both hold.
"""
import os
import sys
import io
import contextlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests import run_gt
from tests import capture_gate

SCRATCH = os.path.join(REPO, ".scratch")
os.makedirs(SCRATCH, exist_ok=True)

# A transpile() stand-in that yields a strategy raising on EVERY bar.
_THROWING_SRC = (
    "def strategy(*a, **kw):\n"
    "    raise RuntimeError('boom always')\n"
)


def _silence(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*a, **kw)


def test_throwing_detected():
    """An always-throwing strategy => throw_count == total_bars > 0."""
    out = os.path.join(SCRATCH, "failloud_throwing.csv")
    orig = run_gt.transpile
    run_gt.transpile = lambda src, trace=True: _THROWING_SRC
    try:
        _silence(run_gt.run_gt, "GT1", max_bars=50, output_path=out, warmup=0)
    finally:
        run_gt.transpile = orig
    throws = run_gt.read_throws(out)
    assert throws is not None, "throw sidecar missing"
    assert throws["total_bars"] > 0, "no bars ran"
    assert throws["throw_count"] == throws["total_bars"], (
        f"detection failed: throw_count={throws['throw_count']} "
        f"total_bars={throws['total_bars']}")
    assert throws["first_traceback"] and "boom always" in throws["first_traceback"]
    print(f"  [ok] throwing detected: {throws['throw_count']}/{throws['total_bars']} bars")


def test_strict_reraises():
    """PL_STRICT=1 => run_gt re-raises on the first throw."""
    out = os.path.join(SCRATCH, "failloud_strict.csv")
    orig = run_gt.transpile
    run_gt.transpile = lambda src, trace=True: _THROWING_SRC
    os.environ["PL_STRICT"] = "1"
    raised = False
    try:
        _silence(run_gt.run_gt, "GT1", max_bars=50, output_path=out, warmup=0)
    except Exception as e:
        raised = True
        assert "boom always" in str(e), f"unexpected exception: {e!r}"
    finally:
        run_gt.transpile = orig
        os.environ.pop("PL_STRICT", None)
    assert raised, "PL_STRICT=1 did not re-raise on the first throw"
    print("  [ok] PL_STRICT=1 re-raises on first throw")


def test_real_run_clean():
    """A real GT run (GT1) throws on ZERO bars."""
    out = os.path.join(SCRATCH, "failloud_gt1.csv")
    os.environ.pop("PL_STRICT", None)
    _silence(run_gt.run_gt, "GT1", max_bars=200, output_path=out, warmup=300)
    throws = run_gt.read_throws(out)
    assert throws is not None, "throw sidecar missing"
    assert throws["throw_count"] == 0, (
        f"real GT1 run threw on {throws['throw_count']}/{throws['total_bars']} "
        f"bars; first traceback:\n{throws['first_traceback']}")
    print(f"  [ok] real GT1 clean: 0/{throws['total_bars']} bars")


def main():
    # Every test drives run_gt.run_gt("GT1", ...), which iterates the pinned GT1
    # MultiCharts capture. In a captures-less clone SKIP and return success; when
    # present, run exactly as before.
    if not capture_gate.captures_present():
        print(capture_gate.skip_message("FAIL-LOUD tests"))
        return 0
    tests = [test_throwing_detected, test_strict_reraises, test_real_run_clean]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL FAIL-LOUD TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
