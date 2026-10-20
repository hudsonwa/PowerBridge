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

"""The core refuses a native kernel whose fill-log version is not its own (#78).

A fake ``pl_transpiler._accel`` stands in for an old or newer wheel, so these
tests run without the extension built. A mismatched or unversioned kernel must
raise ``AccelUnavailable`` before any FFI call, and ``run_oracle`` must then
return the Python result, never the kernel's fills.
"""
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pl_transpiler  # noqa: E402
from pl_transpiler.runtime import accel  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

NQ = get_config("NQ")
SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")


def _install_fake(monkeypatch, **attrs):
    fake = types.ModuleType("pl_transpiler._accel")

    def run_program(*args, **kwargs):
        raise AssertionError("a mismatched kernel must never be called")

    fake.run_program = run_program
    for name, value in attrs.items():
        setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, "pl_transpiler._accel", fake)
    monkeypatch.setattr(pl_transpiler, "_accel", fake, raising=False)
    return fake


@pytest.mark.parametrize("attrs, needle", [
    ({"FILL_LOG_VERSION": 1, "__version__": "0.3.0"},
     "powerbridge-accel 0.3.0 implements fill-log v1"),
    ({"__version__": "0.3.0"}, "no FILL_LOG_VERSION"),
    ({"FILL_LOG_VERSION": accel.FILL_LOG_VERSION + 1}, "(unknown version)"),
])
def test_mismatched_kernel_falls_back_to_python(monkeypatch, capsys, attrs, needle):
    gt2 = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLES)
    python = run_oracle(gt2, bars, NQ)
    _install_fake(monkeypatch, **attrs)
    assert not accel.accel_available()
    before = accel.fallback_count()
    with pytest.raises(accel.AccelUnavailable) as exc:
        accel.run_native(gt2, bars, NQ)
    assert needle in str(exc.value)
    assert "needs fill-log v%d" % accel.FILL_LOG_VERSION in str(exc.value)
    assert accel.fallback_count() == before + 1
    result = run_oracle(gt2, bars, NQ, use_native=True)
    assert result["text"] == python["text"]
    assert not result.get("native")
    assert needle in capsys.readouterr().err


def test_matching_version_is_accepted(monkeypatch):
    fake = _install_fake(monkeypatch, FILL_LOG_VERSION=accel.FILL_LOG_VERSION)
    assert accel.accel_available()
    assert accel._import_accel() is fake
