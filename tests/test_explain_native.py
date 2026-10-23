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

"""``accel.explain_native`` and ``pl_oracle --explain-native``.

The list is every construct the native compile pass refuses, with the source
line of each, and its first entry is the refusal ``run_native`` raises. The
native tests skip when the extension is not built; the "not installed" path
is tested with the import blocked.
"""
import builtins
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from pl_transpiler.runtime import accel as accel_mod  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    AccelUnavailable,
    accel_available,
    explain_native,
    fallback_count,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

NQ = get_config("NQ")
SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
MA_CROSS = os.path.join(REPO, "examples", "ma_cross.el")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")

# Three refusals on lines 3, 8 and 11; the order on line 7 is native.
THREE = (
    "Vars: x(0), i(0);\n"
    "x = Close;\n"
    "While x > 0 Begin\n"
    "    x = x - 1;\n"
    "End;\n"
    "If Close > Open Then\n"
    "    Buy (\"L\") next bar at market;\n"
    "Switch (i) Begin\n"
    "    Case 1: x = 2;\n"
    "End;\n"
    "x = XAverage(Close, 10);\n"
)

# Refusals nested in an If body and a For body, after a refused condition.
NESTED = (
    "Vars: x(0), k(0);\n"
    "If XAverage(Close, 5) > 0 Then Begin\n"
    "    x = Highest(High, 3);\n"
    "End;\n"
    "For k = 1 To 3 Begin\n"
    "    x = Lowest(Low, 3);\n"
    "End;\n"
)


def _require_accel():
    if not accel_available():
        pytest.skip(ACCEL_SKIP)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.needs_native
def test_public_examples_are_fully_native():
    _require_accel()
    assert explain_native(_read(MA_CROSS)) == []
    assert explain_native(_read(GT2)) == []


@pytest.mark.needs_native
def test_every_refusal_is_listed_with_its_line():
    _require_accel()
    got = [(r.kind, r.name, r.line) for r in explain_native(THREE)]
    assert got == [
        ("statement", "while", 3),
        ("statement", "switch", 8),
        ("function", "xaverage", 11),
    ]


@pytest.mark.needs_native
def test_nested_bodies_are_still_checked():
    _require_accel()
    got = [(r.kind, r.name, r.line) for r in explain_native(NESTED)]
    assert got == [
        ("function", "xaverage", 2),
        ("function", "highest", 3),
        ("function", "lowest", 6),
    ]


@pytest.mark.needs_native
@pytest.mark.parametrize("source", [THREE, NESTED])
def test_first_entry_is_the_run_native_refusal(source):
    _require_accel()
    first = explain_native(source)[0]
    with pytest.raises(AccelRefused) as exc:
        run_native(source, load_bars_csv(SAMPLES), NQ)
    assert str(exc.value) == first.message
    assert first.name in first.message.lower()


@pytest.mark.needs_native
def test_refused_variable_is_reported_once():
    _require_accel()
    source = "Vars: v(Close);\nv = v + 1;\nPlot1(v);\n"
    got = [(r.kind, r.name, r.line) for r in explain_native(source)]
    assert got == [("variable", "v", 1)]


@pytest.mark.needs_native
def test_explain_runs_nothing_and_is_not_a_fallback():
    _require_accel()
    before = fallback_count()
    explain_native(THREE)
    assert fallback_count() == before


@pytest.mark.needs_native
def test_cli_lists_refusals_sorted_by_line(tmp_path, capsys):
    _require_accel()
    el = tmp_path / "three.el"
    el.write_text(THREE, encoding="utf-8")
    assert pl_oracle.main(["--explain-native", str(el)]) == 0
    assert capsys.readouterr().out == (
        "statement while 3\nstatement switch 8\nfunction xaverage 11\n")


@pytest.mark.needs_native
def test_cli_says_when_native_can_run_it(capsys):
    _require_accel()
    assert pl_oracle.main(["--explain-native", MA_CROSS]) == 0
    assert capsys.readouterr().out == "native kernel can run this program\n"


def test_not_installed_is_a_clear_error(monkeypatch, capsys):
    real_import = builtins.__import__

    def no_accel(name, *args, **kwargs):
        if name == "pl_transpiler._accel":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "pl_transpiler._accel", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_accel)
    with pytest.raises(AccelUnavailable, match="native kernel not installed"):
        accel_mod.explain_native(THREE)
    assert pl_oracle.main(["--explain-native", MA_CROSS]) == 2
    assert "native kernel not installed" in capsys.readouterr().err
