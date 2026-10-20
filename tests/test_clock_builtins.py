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
"""Computer-clock builtins (CurrentTime_s, CurrentDate, ComputerDateTime, ...)
read the local clock. The clock is pinned here so format and value are exact."""
import datetime

import pytest

from pl_transpiler.runtime import _rt_core
from pl_transpiler.runtime import _rt_datetime
from pl_transpiler.runtime import pl_runtime as rt

FIXED = datetime.datetime(2024, 3, 7, 9, 5, 4, 250000)


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(_rt_datetime, "_clock_now", lambda: FIXED)


def test_formats(fixed_clock):
    assert rt.pl_currenttime_s() == 90504
    assert rt.pl_currenttime() == 905
    assert rt.pl_currentdate() == 1240307
    assert rt.pl_currentdate_s() == 1240307


def test_computerdatetime_is_ole_days(fixed_clock):
    # 2024-03-07 is day 45358 counted from 1899-12-30.
    secs = 9 * 3600 + 5 * 60 + 4.25
    assert rt.pl_computerdatetime() == pytest.approx(45358 + secs / 86400.0)
    assert rt.pl_datetojulian(rt.pl_currentdate()) == 45358


def test_lastcalc_fallback_uses_clock(fixed_clock):
    assert rt.pl_lastcalcjdate() == 45358
    assert rt.pl_lastcalcdatetime() == pytest.approx(rt.pl_computerdatetime())
    assert rt.pl_lastcalcmmtime() == 9 * 60 + 5
    assert rt.pl_lastcalcsstime() == 9 * 3600 + 5 * 60 + 4
    assert rt.pl_lastcalcmstime() == (9 * 3600 + 5 * 60 + 4) * 1000 + 250


def test_not_treated_as_unimplemented(fixed_clock, monkeypatch):
    monkeypatch.setattr(_rt_core, "_PL_STRICT", True)
    names = ["pl_currenttime_s", "pl_currentdate_s", "pl_currenttime",
             "pl_currentdate", "pl_computerdatetime", "pl_lastcalcdatetime",
             "pl_lastcalcjdate", "pl_lastcalcmmtime", "pl_lastcalcmstime",
             "pl_lastcalcsstime"]
    before = dict(_rt_core._UNIMPL_CALLS)
    for n in names:
        assert getattr(rt, n)() != 0
    assert _rt_core._UNIMPL_CALLS == before


def test_generated_code_calls_clock(fixed_clock, monkeypatch):
    import os

    from pl_transpiler import transpile
    from pl_transpiler.runtime.instrument_config import get_config
    from pl_transpiler.tools.pl_run import load_bars_csv, run_el

    src = ("Vars: a(0), b(0), c(0);\n"
           "a = CurrentTime_s; b = ComputerDateTime; c = CurrentDate_s;\n")
    code = transpile(src)
    assert "pl_currenttime_s()" in code and "pl_computerdatetime()" in code
    monkeypatch.setattr(_rt_core, "_PL_STRICT", True)
    bars = load_bars_csv(os.path.join(os.path.dirname(__file__), "..",
                                      "examples", "NQ_sample_bars.csv"))[:5]
    result = run_el(src, bars, get_config("NQ"))
    row = dict(zip(result["columns"], result["rows"][-1]))
    assert float(row["a"]) == 90504
    assert float(row["c"]) == 1240307
    assert int(float(row["b"])) == 45358
