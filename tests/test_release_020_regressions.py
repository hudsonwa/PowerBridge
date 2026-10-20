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
"""Regressions found by the 0.2.0 release audit (ES MultiCharts parity run)."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.runtime import accel  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.runtime.pl_runtime import pl_datetojulian, pl_formatdate  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))


def _value_at(src, column, bar=31):
    return run_el(src, BARS, NQ, trace_columns=[column])["rows"][bar][0]


def test_input_shadows_reserved_keyword():
    # An input named ExitTime must not compile to the ExitTime position keyword.
    py = transpile("Inputs: ExitTime(1458); If Time >= ExitTime Then Buy Next Bar At Market;")
    assert "kwargs.get('exit_time'" not in py
    assert "time[-1] >= exittime" in py


def test_input_bar_reference_is_its_value():
    assert _value_at("Inputs: N(5); Vars: v(0); v = N[1] + N[3];", "v") == "10.0"


def test_input_named_like_engine_key_keeps_its_default():
    # Len/Prec used to be overwritten by engine-wide constants (14 / 6).
    assert _value_at("Inputs: Len(5), Prec(2); Vars: v(0); v = Len + Prec;", "v") == "7.0"


def test_formatdate_accepts_serial_datetime():
    assert pl_formatdate("yyyy-MM-dd ddd", pl_datetojulian(1160126)) == "2016-01-26 Tue"
    assert pl_formatdate("yyyy-MM-dd", 20160126.5) == "2016-01-26"
    assert pl_formatdate("yyyy-MM-dd", pl_datetojulian(0))  # no crash on EntryDate()=0


def test_unimplemented_builtins_are_reported(capsys):
    out = run_el("Vars: x(0); Arrays: a[5](0); x = ArraySize(a) + Random(3);", BARS, NQ)
    assert set(out["unimplemented"]) == {"arraysize", "random"}
    assert "unimplemented EasyLanguage builtin" in capsys.readouterr().err


def test_native_unguarded_entry_does_not_double():
    # Rust once sized a qty-0 entry off the open position: 2**n fills, unbounded RAM.
    if not accel.accel_available():
        pytest.skip(ACCEL_SKIP)
    src = "If Close > 0 Then Buy Next Bar At Market;"
    bars = BARS[:30]
    native = accel.run_native(src, bars, NQ)
    python = run_el(src, bars, NQ)["trades"]
    assert len(native) == len(python) == 29
