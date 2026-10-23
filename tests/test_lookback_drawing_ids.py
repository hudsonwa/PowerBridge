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

"""Issue #159: a drawing id declared ``Vars: ID(-1)`` keeps its numeric type,
and Date/Time lookbacks past the start of the tape read 0 for every N (as
``Date[1]`` on bar 1 always has, and as the native kernel does), on both paths.
"""
import os

import pytest

from pl_transpiler import transpile
from pl_transpiler.runtime.accel import accel_available
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_oracle import run_oracle
from pl_transpiler.tools.pl_run import load_bars_csv
from tests.skip_messages import ACCEL_SKIP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NQ = get_config("NQ")
TAPE = {"name": "sample", "sha256": "a" * 64, "tick": 0.25}

_NEW = {
    "tl": ("TL_New(Date[1], Time[1], Close[1], Date, Time, Close)",
           ("TL_GetEndTime", "TL_GetEndDate")),
    "text": ('Text_New(Date, Time, Close, "x")', ("Text_GetTime", "Text_GetDate")),
    "arw": ("Arw_New(Date, Time, Close, false)", ("Arw_GetTime", "Arw_GetDate")),
}


def _drawing_source(kind):
    new, (get_time, get_date) = _NEW[kind]
    return (
        "Vars: ID(-1), T(0), D(0);\n"
        f"If CurrentBar = 2 Then ID = {new};\n"
        f"If ID > 0 Then Begin T = {get_time}(ID); D = {get_date}(ID); End;\n"
        "If T > 0 and D > 0 and MarketPosition = 0 Then Buy next bar at market;\n"
        "If MarketPosition = 1 and Close < Close[1] Then Sell next bar at market;\n"
    )


def _lookback_source(series, n):
    return (
        "Vars: X(0);\n"
        f"X = {series}[{n}];\n"
        "If X = 0 and MarketPosition = 0 Then Buy next bar at market;\n"
        "If X > 0 and MarketPosition = 1 Then Sell next bar at market;\n"
    )


@pytest.fixture(scope="module")
def bars():
    loaded = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))
    return loaded[0] if isinstance(loaded, tuple) else loaded


def _runs(source, bars, **kwargs):
    python = run_oracle(source, bars, NQ, **kwargs)
    assert python["fills"] > 0
    if accel_available():
        native = run_oracle(source, bars, NQ, use_native=True, **kwargs)
        assert native["sha256"] == python["sha256"]
    return python


def test_negative_initialiser_is_numeric():
    code = transpile("Vars: ID(-1), F(-2.5);\nIf ID < 0 Then F = F - 1;\n")
    assert "''" not in code


@pytest.mark.parametrize("kind", sorted(_NEW))
def test_drawing_id_from_negative_init_runs(kind, bars):
    _runs(_drawing_source(kind), bars, drawings=True,
          drawing_case_id=f"g-x-001-{kind}", drawing_tape=TAPE)


@pytest.mark.parametrize("series", ["Date", "Time"])
@pytest.mark.parametrize("n", [1, 5, 200])
def test_out_of_history_lookback_reads_zero(series, n, bars):
    result = _runs(_lookback_source(series, n), bars)
    # Bars 1..n read 0 (buy); once bar n + 1 exists it reads a real value (sell).
    assert result["round_turns"] == (1 if n < len(bars) else 0)


def test_dynamic_out_of_history_lookback_matches_constant(bars):
    fixed = _runs(_lookback_source("Date", 5), bars)
    dynamic = _runs("Inputs: N(5);\n" + _lookback_source("Date", "N"), bars)
    assert dynamic["sha256"] == fixed["sha256"]


@pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)
def test_native_runs_the_lookback_program(bars):
    native = run_oracle(_lookback_source("Date", 200), bars, NQ, use_native=True)
    assert native["native"] is True, native.get("native_fallback_reason")
