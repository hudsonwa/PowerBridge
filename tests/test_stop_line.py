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
"""test_stop_line.py — a strict-mode stop names the line of the statement that
raised it, not the first line (a comment, a string, another order) that merely
mentions the word, on both the native and the pure-Python path."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import run  # noqa: E402
from pl_transpiler.runtime import _rt_core  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402

BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
PATHS = ["--python"] + (["--native"] if accel_available() else [])

NAMED_EXIT = (
    '// Two entries, then a named exit: Sell from entry ("A").\n'
    'If BarNumber = 5 then Buy ("A") 1 contract next bar at market;\n'
    'If BarNumber = 6 then Buy ("B") 1 contract next bar at market;\n'
    'If BarNumber = 7 then BuyToCover ("XS") from entry ("A") next bar at market;\n'
    'Vars: x(0);\n'
    'x = Close;\n'
    'If BarNumber = 10 then Sell ("XA") from entry ("A") next bar at market;\n'
)
BAR_COLUMN = (
    "Vars: x(0);\n"
    "// x = OpenInt would need an openint column\n"
    "Print(\"OpenInt\");\n"
    "x = OpenInt;\n"
)
BUILTIN = (
    "Vars: x(0), ArraySizeSeen(0);\n"
    "Arrays: a[5](0);\n"
    "// ArraySize is not implemented\n"
    "x = ArraySize(a);\n"
)


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(_rt_core._DEFAULT_STATE, "strict", True)


def _stop(tool, path, src, tmp_path, capsys, extra=()):
    el = tmp_path / "s.el"
    el.write_text(src, encoding="utf-8")
    assert tool.main([path, *extra, str(el), BARS_CSV]) == 2
    err = [ln for ln in capsys.readouterr().err.strip().splitlines()
           if not ln.startswith("note: ")]
    assert len(err) == 1
    return err[0]


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
@pytest.mark.parametrize("path", PATHS)
def test_named_exit_stop_names_the_exit_line(tool, path, strict, tmp_path, capsys):
    err = _stop(tool, path, NAMED_EXIT, tmp_path, capsys, ("--config", "NQ"))
    assert "(s.el line 7): " in err and 'from entry ("A")' in err


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
@pytest.mark.parametrize("path", PATHS)
def test_missing_column_stop_names_the_reading_line(tool, path, strict, tmp_path, capsys):
    err = _stop(tool, path, BAR_COLUMN, tmp_path, capsys)
    assert "(s.el line 4): " in err and "'openint' column" in err


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
@pytest.mark.parametrize("path", PATHS)
def test_builtin_stop_names_the_calling_line(tool, path, strict, tmp_path, capsys):
    err = _stop(tool, path, BUILTIN, tmp_path, capsys)
    assert "(s.el line 4): " in err and "'arraysize'" in err


def test_unparsable_source_falls_back_to_first_mention():
    exc = _rt_core.UnimplementedBuiltinError("pl_arraysize")
    assert run._stop_line("x = = ArraySize(a);\n", exc) is None
