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

"""examples/hand_written.py (docs/PYTHON_STRATEGIES.md): a strategy written
by hand in the mirror dialect emits EasyLanguage, backtests the same on the
Python and native paths, and survives EL -> mirror Python -> EL."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.py_front import PyFrontError, py_to_ast  # noqa: E402
from pl_transpiler.reverse import py_to_el  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import el_emit  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars, run_el  # noqa: E402

EXAMPLE = os.path.join(REPO, "examples", "hand_written.py")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
NQ = get_config("NQ")
SHA = "9f11e024f4832be65e4c22641085e47c7ec731357162a801a885de95663f61aa"


def _el():
    with open(EXAMPLE, encoding="utf-8") as f:
        return py_to_el(f.read())


def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


def test_el_emit_accepts_it_and_every_builtin_is_proven(tmp_path, capsys):
    out = tmp_path / "hand_written.el"
    assert el_emit.main([EXAMPLE, "-o", str(out)]) == 0
    report = capsys.readouterr().out
    assert "0 NOT-PROVEN" in report and "OK --" in report
    el = out.read_text()
    assert "Inputs: trendlen( 20 ) ;" in el
    assert "close[dips]" in el
    assert "next bar at exitlevel limit" in el


def test_el_emit_to_stdout_names_the_input(capsys):
    assert el_emit.main([EXAMPLE]) == 0
    out = capsys.readouterr().out
    assert f"{EXAMPLE}: 5 builtins used" in out
    assert ".txt:" not in out


def test_python_runner_backtests_the_emitted_el():
    el, bars = _el(), load_bars(SAMPLE)
    assert run_oracle(el, bars, NQ, use_native=False)["sha256"] == SHA
    out = run_el(el, bars, NQ, trace_columns=["MarketPosition"])
    assert len(out["trades"]) == 6


@pytest.mark.skipif(not accel_available(), reason="native kernel not built")
def test_native_path_gives_the_same_digest():
    result = run_oracle(_el(), load_bars(SAMPLE), NQ, use_native=True)
    assert result.get("native") and result["sha256"] == SHA


def test_emitted_el_round_trips():
    el = _el()
    back = py_to_el(transpile(el, dialect="mirror"))
    assert _lines(back) == _lines(el)
    bars = load_bars(SAMPLE)
    assert run_oracle(back, bars, NQ, use_native=False)["sha256"] == SHA


@pytest.mark.parametrize("body,message", [
    ("    x = f32(0)\n    x = sum(close[-5:]) / 5\n", "unsupported function sum"),
    ("    n = kwargs.get('Len', 3)\n", "unrecognised kwargs.get key 'Len'"),
])
def test_guide_rejections(body, message):
    src = "def strategy(open, high, low, close, volume, date, time, **kwargs):\n" + body
    with pytest.raises(PyFrontError, match=message):
        py_to_ast(src)
