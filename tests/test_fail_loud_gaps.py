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
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.errors import UnimplementedKeywordError  # noqa: E402
from pl_transpiler.runtime import _rt_core  # noqa: E402
from pl_transpiler.runtime._rt_core import MissingBarColumnError, pl_bar_column  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run, pl_transpile  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
BARS = load_bars_csv(BARS_CSV)[:20]


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(_rt_core, "_PL_STRICT", True)


def _write(tmp_path, src):
    path = tmp_path / "s.el"
    path.write_text(src, encoding="utf-8")
    return str(path)


# ---- arity ----

@pytest.mark.parametrize("call, got", [
    ("Stochastic", 0), ("Stochastic(Close)", 1),
    ("Stochastic(High, Low, Close, 14, 3, 3, 1, a, b, c, d, e)", 12),
])
def test_stochastic_wrong_arg_count_fails_at_transpile(call, got):
    src = "Vars: x(0), a(0), b(0), c(0), d(0), e(0);\nx = %s;\n" % call
    with pytest.raises(UnimplementedKeywordError) as exc:
        transpile(src)
    assert str(exc.value) == "'Stochastic' at line 2 takes 11 arguments, got %d" % got


def test_zero_arg_call_to_catalogued_function_fails():
    with pytest.raises(UnimplementedKeywordError) as exc:
        transpile("Vars: x(0);\nx = 1;\nx = MaxList;\n")
    assert "'MaxList' at line 3 takes at least" in str(exc.value)


def test_well_formed_calls_still_transpile():
    transpile("Vars: x(0), a(0), b(0), c(0), d(0);\n"
              "x = Stochastic(High, Low, Close, 14, 3, 3, 1, a, b, c, d);\n"
              "x = MaxList(Close, Open);\n")


@pytest.mark.parametrize("tool", [pl_run, pl_oracle, pl_transpile])
def test_cli_reports_bad_arity_without_traceback(tool, tmp_path, capsys):
    el = _write(tmp_path, "Vars: x(0);\nx = Stochastic;\n")
    args = [el] if tool is pl_transpile else [el, BARS_CSV]
    assert tool.main(args) != 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "'Stochastic' at line 2 takes 11 arguments, got 0" in err


# ---- Ticks / UpTicks / DownTicks / OpenInt ----

@pytest.mark.parametrize("word, column", [
    ("Ticks", "ticks"), ("UpTicks", "upticks"),
    ("DownTicks", "downticks"), ("OpenInt", "openint"),
])
def test_missing_bar_column_stops_run(strict, word, column):
    src = "Vars: x(0);\nx = %s;\nif x > 5 then buy next bar at market;\n" % word
    with pytest.raises(MissingBarColumnError) as exc:
        run_el(src, BARS, NQ)
    assert exc.value.builtin == word
    assert "'%s' column" % column in str(exc.value)


def test_bar_column_supplied_by_data_is_used(strict):
    assert pl_bar_column({"ticks": 7}, "ticks", "Ticks") == 7


def test_bar_column_lenient_reads_zero_and_warns(monkeypatch, capsys):
    monkeypatch.setattr(_rt_core, "_PL_STRICT", False)
    _rt_core.pl_unimplemented_reset()
    assert pl_bar_column({}, "openint", "OpenInt") == 0
    assert _rt_core.pl_unimplemented_warn("t") == {"openint": 1}
    assert "openint" in capsys.readouterr().err
    _rt_core.pl_unimplemented_reset()


def test_cli_reports_missing_column_on_one_line(strict, tmp_path, capsys):
    el = _write(tmp_path, "Vars: x(0);\nx = OpenInt;\nif x > 0 then buy next bar at market;\n")
    assert pl_run.main([el, BARS_CSV]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith("pl_run: error (s.el line 2): ")
    assert "'openint' column" in err[0]


# ---- python -m pl_transpiler.catalog ----

def test_catalog_module_runs_without_runpy_warning():
    proc = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "pl_transpiler.catalog",
         "--search", "date"],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert "CurrentDate" in proc.stdout.split()
