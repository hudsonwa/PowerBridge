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

"""EasyLanguage ends every statement and declaration with ';'. A missing
one is a syntax error; the only place it is left out is before `Else`."""
import os
import subprocess
import sys

import pytest

from pl_transpiler import transpile
from pl_transpiler.lexer import PLSyntaxError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MISSING = (
    # (source, line of the statement missing its ';', token it follows)
    ("Vars: x(0)\nx = Close;\n", 1, "')'"),
    ("Inputs: Len(5)\nVars: x(0);\n", 1, "')'"),
    ("Arrays: a[3](0)\nVars: x(0);\n", 1, "')'"),
    ("Vars: x(0);\nx = Close\nx = Open;\n", 2, "'close'"),
    ("Vars: x(0);\nx = Close", 2, "'close'"),
    ("If Close > 0 Then Buy Next Bar At Market\nSell Next Bar At Market;\n", 1, "'market'"),
    ("Vars: x(0);\nIf Close > 0 Then Begin\n  x = 1\nEnd;\n", 3, "'1'"),
    ("Vars: x(0);\nIf Close > 0 Then Begin\n  x = 1;\nEnd\nx = 2;\n", 4, "'end'"),
    ("Vars: x(0), i(0);\nFor i = 1 To 3 Begin x = x + i End;\n", 2, "'i'"),
    ("Vars: x(0);\nOnce x = 1\nx = 2;\n", 2, "'1'"),
    ("Vars: x(0);\nSwitch (x) Begin\n  Case 1: x = 2\nEnd;\n", 3, "'2'"),
    ("Vars: x(0);\nSwitch (x) Begin\n  Case 1: x = 2;\nEnd\n", 4, "'end'"),
    ("Vars: x(0);\nPlot1(Close)\n", 2, "')'"),
    ("Vars: x(0);\nSetStopLoss(100)\n", 2, "')'"),
)


@pytest.mark.parametrize("src,line,after", _MISSING)
def test_missing_semicolon_is_a_syntax_error(src, line, after):
    with pytest.raises(PLSyntaxError) as info:
        transpile(src)
    msg = str(info.value)
    assert f"at line {line}:" in msg
    assert "missing ';' at the end of the statement" in msg
    assert f"hint: add ';' after {after}" in msg
    assert "^" in msg


_VALID = (
    # No ';' before Else, after a simple statement or after End.
    "Vars: x(0);\nIf Close > Open Then x = 1 Else x = 2;\n",
    "Vars: x(0);\nIf Close > Open Then Begin\n  x = 1;\nEnd Else Begin\n  x = 2;\nEnd;\n",
    "Vars: x(0);\nIf Close > Open Then Buy Next Bar At Market Else Sell Short Next Bar At Market;\n",
    "Vars: x(0);\nIf Close > Open Then If Close > 1 Then x = 1 Else x = 2;\n",
    # Attributes take no ';'.
    "[IntrabarOrderGeneration = False]\nVars: x(0);\nx = Close;\n",
    # Stray ';' stays allowed.
    "Vars: x(0);;\nx = Close;;\n",
)


@pytest.mark.parametrize("src", _VALID)
def test_documented_omissions_still_parse(src):
    transpile(src)


def test_cli_reports_missing_semicolon_with_exit_2(tmp_path):
    el = tmp_path / "s.el"
    el.write_text("Vars: x(0)\nx = Close\nIf x > 0 Then Buy Next Bar At Market\n")
    proc = subprocess.run(
        [sys.executable, "-m", "pl_transpiler.tools.pl_transpile", str(el)],
        cwd=REPO, env=dict(os.environ, PB_NO_PBAR="1"),
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "line 1:" in proc.stderr
    assert "missing ';' at the end of the statement" in proc.stderr
    assert proc.stdout == ""
