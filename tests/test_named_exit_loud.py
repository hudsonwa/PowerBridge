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
"""Malformed calls and missing bar columns stop with one clear line, not a guess."""
"""test_named_exit_loud.py — `from entry("X")` with several entries open (#24).

The engine closes the whole position for a named exit, but EasyLanguage
documents it as closing only entry X. No MultiCharts capture confirms those
fills, so a named exit that would fill while more than one entry is open in
its direction stops the run (strict) or warns (lenient). One open entry keeps
today's fills. The native kernel runs a named exit with one entry open and
refuses the run when several are (#125), so `use_native` falls back to the
Python engine and raises the same error.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime import _rt_core  # noqa: E402
from pl_transpiler.runtime._rt_core import NamedExitUnverifiedError  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

NQ = get_config("NQ")
BARS = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))[:40]

TWO_LONG = (
    'If BarNumber = 5 then Buy ("A") 1 contract next bar at market;\n'
    'If BarNumber = 6 then Buy ("B") 1 contract next bar at market;\n'
    'If BarNumber = 10 then Sell ("XA") from entry ("A") next bar at market;\n'
)
TWO_SHORT = (
    'If BarNumber = 5 then SellShort ("A") 1 contract next bar at market;\n'
    'If BarNumber = 6 then SellShort ("B") 1 contract next bar at market;\n'
    'If BarNumber = 10 then BuyToCover ("XA") from entry ("A") next bar at market;\n'
)
ONE_LONG = (
    'If BarNumber = 5 then Buy ("A") 1 contract next bar at market;\n'
    'If BarNumber = 10 then Sell ("XA") from entry ("A") next bar at market;\n'
)
# The same program with an untagged exit: the reference for "unchanged fills".
ONE_LONG_UNTAGGED = ONE_LONG.replace(' from entry ("A")', '')
NOT_OPEN = ONE_LONG.replace('entry ("A")', 'entry ("Z")')
# Entry A pyramided: every open entry is the one the exit names.
PYRAMID_A = (
    'If BarNumber = 5 then Buy ("A") 1 contract next bar at market;\n'
    'If BarNumber = 6 then Buy ("A") 1 contract next bar at market;\n'
    'If BarNumber = 10 then Sell ("XA") from entry ("A") next bar at market;\n'
)
PYRAMID_A_TOTAL = PYRAMID_A.replace(
    'Sell ("XA") from', 'Sell ("XA") 1 contract total from')

PATHS = [False] + ([True] if accel_available() else [])


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(_rt_core, "_PL_STRICT", True)


@pytest.mark.parametrize("native", PATHS)
@pytest.mark.parametrize("src,verb", [(TWO_LONG, "Sell"), (TWO_SHORT, "BuyToCover")])
def test_two_entries_named_exit_stops(strict, src, verb, native):
    with pytest.raises(NamedExitUnverifiedError) as exc:
        run_oracle(src, BARS, NQ, use_native=native)
    msg = str(exc.value)
    assert verb in msg and 'from entry ("A")' in msg and "#24" in msg


def test_two_entries_named_exit_lenient_flattens(monkeypatch):
    monkeypatch.setattr(_rt_core, "_PL_STRICT", False)
    _rt_core.pl_unimplemented_reset()
    res = run_oracle(TWO_LONG, BARS, NQ)
    assert res["fills"] == 4  # today's behaviour: both entries closed
    assert _rt_core.pl_unimplemented_report().get("pl_from entry") == 1
    _rt_core.pl_unimplemented_reset()


@pytest.mark.parametrize("native", PATHS)
@pytest.mark.parametrize("src", [ONE_LONG, NOT_OPEN])
def test_single_entry_named_exit_unchanged(strict, src, native):
    got = run_oracle(src, BARS, NQ, use_native=native)
    ref = run_oracle(ONE_LONG_UNTAGGED, BARS, NQ)
    assert got["fills"] == 2
    assert got["sha256"] == ref["sha256"]
    if native:
        assert got.get("native_fallback_reason") is None


@pytest.mark.parametrize("native", PATHS)
@pytest.mark.parametrize("src,fills", [(PYRAMID_A, 4), (PYRAMID_A_TOTAL, 3)])
def test_pyramided_named_entry_exit_unchanged(strict, src, fills, native):
    got = run_oracle(src, BARS, NQ, use_native=native)
    ref = run_oracle(src.replace(' from entry ("A")', ''), BARS, NQ)
    assert got["fills"] == fills
    assert got["sha256"] == ref["sha256"]
    if native:
        assert got.get("native_fallback_reason") is None
