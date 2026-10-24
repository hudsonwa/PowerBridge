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

"""Native-vs-Python parity for a logging section (issue #138).

``StrLen``, ``FormatDate``, ``NewLine``, ``Text_SetColor`` and the
``FileAppend``/``FileDelete`` stubs run natively. The Python stubs write
nothing, so neither path may create a file. A string mixed with a number is a
``TypeError`` in Python; the kernel refuses it instead of converting.

Skips cleanly without the native extension.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    accel_available,
    explain_native,
    run_native,
)
from pl_transpiler.runtime.drawing_dump import canonical  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.test_accel_trace_parity import assert_trace_parity  # noqa: E402

pytestmark = [pytest.mark.needs_native,
              pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)]

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
FIXTURE = os.path.join(REPO, "accel", "src", "strategy", "fixtures", "strings_log.txt")
TAPE = {"name": "sample", "sha256": "a" * 64, "tick": 0.25}

with open(FIXTURE, encoding="utf-8") as _f:
    LOG = _f.read()

# Non-ASCII StrLen, the short FormatDate forms and a variable named NewLine.
SHAPES = """
Vars: U(""), K(0), NewLine(3), F("");
U = "été€" + FormatDate("yy") + FormatDate("MMM d, h:mm t", 45000.5);
K = StrLen(U) + NewLine;
F = FormatDate("dddd HH:mm:ss", Date + Time / 2400);
If K = 22 and StrLen(F) > 15 and MarketPosition = 0 Then Buy next bar at market;
If MarketPosition = 1 and Close < Close[1] Then Sell next bar at market;
"""

# Text_SetColor of an id that was never created: Python without a recorder
# returns 0; the kernel refuses at run time and the Python path runs instead.
LATE_REFUSAL = LOG + """
If CurrentBar = 60 Then Text_SetColor(999, 255);
"""


def _load(path):
    loaded = load_bars_csv(path)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.fixture(scope="module")
def bars():
    return _load(BARS_CSV)


@pytest.mark.parametrize("source", [LOG, SHAPES], ids=["log", "shapes"])
def test_native_digest_equals_python(source, bars):
    want = run_oracle(source, bars, NQ, max_bars_back=0)
    assert want["fills"] > 0, "the program must trade for the digest to mean anything"
    got = run_oracle(source, bars, NQ, use_native=True, max_bars_back=0)
    assert got["native"] is True, got.get("native_fallback_reason")
    assert got["text"] == want["text"]
    assert fills_sha256(run_native(source, bars, NQ)) == want["sha256"]


@pytest.mark.parametrize("source", [LOG, SHAPES], ids=["log", "shapes"])
def test_native_trace_equals_python(source, bars):
    assert_trace_parity(source, bars)


def test_native_drawings_equal_python_including_text_color(bars):
    kwargs = dict(drawings=True, drawing_case_id="g-x-001-strings", drawing_tape=TAPE)
    python = run_oracle(LOG, bars, NQ, **kwargs, max_bars_back=0)
    native = run_oracle(LOG, bars, NQ, use_native=True, **kwargs, max_bars_back=0)
    assert native["native"] is True, native.get("native_fallback_reason")
    assert native["sha256"] == python["sha256"]
    native["drawings"]["producer"] = python["drawings"]["producer"]
    assert canonical(native["drawings"]) == canonical(python["drawings"])
    texts = [o for o in python["drawings"]["objects"] if o["type"] == "text"]
    assert texts and all(o["color"] == 255 for o in texts)


def test_file_calls_write_nothing_on_either_path(bars, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    python = run_oracle(LOG, bars, NQ, max_bars_back=0)
    native = run_oracle(LOG, bars, NQ, use_native=True, max_bars_back=0)
    assert native["native"] is True
    assert native["sha256"] == python["sha256"]
    late = run_oracle(LATE_REFUSAL, bars, NQ, use_native=True, max_bars_back=0)
    assert late["native"] is False
    assert "no live text with id 999" in late["native_fallback_reason"]
    assert late["sha256"] == python["sha256"]
    assert os.listdir(tmp_path) == []


def test_explain_lists_none_of_the_logging_calls():
    assert explain_native(LOG) == []
    assert explain_native(SHAPES) == []


@pytest.mark.parametrize("expr", ['"a" + 1', '1 + "a"', '"5" * 2', 'StrLen(Close)'])
def test_string_mixed_with_a_number_is_refused(expr, bars):
    source = f'Vars: S(""), X(0);\nIf CurrentBar = 3 Then X = 1 + 0 * StrLen({expr});\n'
    if "StrLen" in expr:
        source = f'Vars: X(0);\nIf CurrentBar = 3 Then X = {expr};\n'
    with pytest.raises(AccelRefused):
        run_native(source, bars, NQ)
