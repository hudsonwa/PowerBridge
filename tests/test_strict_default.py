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
"""Strict mode is the default: an unimplemented builtin stops the run (issue #2)."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime import _rt_core  # noqa: E402
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
BARS = load_bars_csv(BARS_CSV)[:20]
SRC = "Vars: x(0);\nArrays: a[5](0);\nx = ArraySize(a);\n"


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(_rt_core._DEFAULT_STATE, "strict", True)


@pytest.mark.parametrize("value, expected", [
    (None, True), ("", True), ("1", True), ("yes", True),
    ("0", False), ("false", False), ("No", False), (" off ", False),
])
def test_pl_strict_env_parsing(value, expected):
    env = {} if value is None else {"PL_STRICT": value}
    assert _rt_core._strict_from_env(env) is expected


def test_unimplemented_builtin_stops_run(strict):
    with pytest.raises(UnimplementedBuiltinError) as exc:
        run_el(SRC, BARS, NQ)
    assert exc.value.builtin == "arraysize"
    assert "PL_STRICT=0" in str(exc.value) and "--lenient" in str(exc.value)


def test_cosmetic_builtins_stay_allowed(strict):
    assert _rt_core._unimplemented("pl_playsound") == 0
    assert _rt_core._unimplemented("pl_text_new") == 0
    # A drawing getter must not be cosmetic: returning 0 can change a trade.
    with pytest.raises(UnimplementedBuiltinError):
        _rt_core._unimplemented("pl_tl_getvalue")


def _write(tmp_path, src):
    path = tmp_path / "s.el"
    path.write_text(src, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
def test_cli_prints_one_line_error_with_source_line(tool, strict, tmp_path, capsys):
    el = _write(tmp_path, SRC)
    assert tool.main([el, BARS_CSV]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith(tool.__name__.rsplit(".", 1)[1] + ": error (s.el line 3): ")
    assert "'arraysize'" in err[0] and "--lenient" in err[0]


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
def test_cli_lenient_flag_warns_and_continues(tool, strict, tmp_path, capsys):
    el = _write(tmp_path, SRC)
    extra = ["--python"] if tool is pl_oracle else ["-o", str(tmp_path / "out.csv")]
    assert tool.main([el, BARS_CSV, "--lenient"] + extra) == 0
    assert "WARNING" in capsys.readouterr().err
    assert _rt_core._DEFAULT_STATE.strict is False
