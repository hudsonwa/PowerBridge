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
"""test_runtime_errors.py — an error raised while the generated strategy runs
(array index out of range, division by zero, anything else) stops with one
line naming the EL source line, exit 2, on both CLIs and both paths; the
library raises ELRuntimeError with ``.line``."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import ELRuntimeError, transpile  # noqa: E402
from pl_transpiler.errors import el_runtime_error  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
PATHS = ["--python"] + (["--native"] if accel_available() else [])

LITERAL_INDEX = "Arrays: a[3](0);\na[4] = Close;\n"
VARIABLE_INDEX = (
    "Arrays: MyArr[3](0);\n"
    "Vars: i(0);\n"
    "i = i + 1;\n"
    "If i > 0 then begin\n"
    "  Value1 = 1;\n"
    "  Value2 = MyArr[i] + MyArr[i - 1];\n"
    "end;\n"
)
DIVISION = "Vars: x(0);\nx = Close / (BarNumber - 1);\n"
MODULO = "// Mod by zero\nValue1 = Mod(5, BarNumber - 1);\n"


def _stop(tool, path, src, tmp_path, capsys):
    el = tmp_path / "s.el"
    el.write_text(src, encoding="utf-8")
    assert tool.main([path, str(el), BARS_CSV]) == 2
    err = [ln for ln in capsys.readouterr().err.strip().splitlines()
           if not ln.startswith("note: ")]
    assert len(err) == 1, err
    return err[0]


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("src, message", [
    (LITERAL_INDEX, "array index 4 out of range for 'a' (max index 3) at line 2"),
    (VARIABLE_INDEX, "array index 4 out of range for 'MyArr' (max index 3) at line 6"),
    (DIVISION, "division by zero at line 2"),
    (MODULO, "division by zero at line 2"),
])
def test_cli_names_the_el_line(tool, path, src, message, tmp_path, capsys):
    prog = "pl_run" if tool is pl_run else "pl_oracle"
    assert _stop(tool, path, src, tmp_path, capsys) == f"{prog}: error: {message}"


def test_pb_debug_shows_the_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("PB_DEBUG", "1")
    el = tmp_path / "s.el"
    el.write_text(LITERAL_INDEX, encoding="utf-8")
    with pytest.raises(ELRuntimeError):
        pl_oracle.main(["--python", str(el), BARS_CSV])


@pytest.mark.parametrize("use_native", [False] + ([True] if accel_available() else []))
def test_library_raises_el_runtime_error(use_native):
    bars = load_bars_csv(BARS_CSV)
    with pytest.raises(ELRuntimeError) as info:
        pl_oracle.run_oracle(LITERAL_INDEX, bars, get_config("NQ"), use_native=use_native)
    assert info.value.line == 2
    assert isinstance(info.value.__cause__, IndexError)
    with pytest.raises(ELRuntimeError) as info:
        pl_run.run_el(DIVISION, bars, get_config("NQ"))
    assert info.value.line == 2
    assert isinstance(info.value.__cause__, ZeroDivisionError)


def test_generic_error_names_the_line(monkeypatch):
    src = transpile("Vars: x(0);\n\nx = Average(Close, 3);\n", persist=True)
    ns = {}
    exec(compile(src, "<gen>", "exec"), ns)

    def boom(*args, **kwargs):
        raise ValueError("bad value")
    for name in list(ns):
        if name.startswith("pl_average"):
            ns[name] = boom
    with pytest.raises(ValueError) as info:
        ns["strategy"]([1.0], [1.0], [1.0], [1.0], [1.0], [1], [1])
    err = el_runtime_error(info.value, src, "<gen>")
    assert str(err) == ("internal error at line 3: ValueError: bad value "
                        "(set PB_DEBUG=1 for the full traceback)")
    assert err.line == 3


def test_errors_outside_generated_code_pass_through():
    try:
        raise IndexError("not from the strategy")
    except IndexError as exc:
        assert el_runtime_error(exc, "", "<gen>") is None


def test_generated_source_is_unchanged_text():
    src = transpile(LITERAL_INDEX, persist=True)
    assert isinstance(src, str) and str(src) == src
    assert set(src.el_line_map.values()) == {1, 2}  # the declaration and the assignment
