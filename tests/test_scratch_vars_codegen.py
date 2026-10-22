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

"""Undeclared Value0..Value99 / Condition0..Condition99 on the Python path.

EL predeclares them as numeric / boolean variables that start at 0 / false
and keep their value across bars, so they behave exactly like
``Vars: ValueN(0), ConditionN(false);``. No native extension needed.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.lexer import tokenise  # noqa: E402
from pl_transpiler.parser import parse  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.semantic import add_scratch_decls  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))


def _ast(source):
    tokens, positions = tokenise(source)
    return parse(tokens, positions=positions, source=source)


def test_undeclared_names_get_one_sorted_decl():
    ast = _ast("Vars: Value1(5); Value2 = Value1; "
               "for Condition3 = 0 to 1 begin end; Value10 = 0;")
    out = add_scratch_decls(ast)
    assert out["body"][0] == {"type": "var_decl", "decls": [
        {"name": "condition3", "init": {"type": "boolean", "value": False}},
        {"name": "value10", "init": {"type": "number", "value": "0"}},
        {"name": "value2", "init": {"type": "number", "value": "0"}},
    ]}
    assert out["body"][1:] == ast["body"]


def test_program_without_scratch_names_is_unchanged():
    ast = _ast("Vars: Value1(5), x(0); x = Value1; Value100 = 1;")
    assert add_scratch_decls(ast) is ast


def test_codegen_matches_explicit_declaration():
    src = "Value1 = Value1 + 1; Condition1 = not Condition1;"
    explicit = "Vars: Condition1(false), Value1(0);\n" + src
    assert transpile(src) == transpile(explicit)


def test_read_before_write_starts_at_zero_and_accumulates():
    src = ("if Value1 = 0 and not Condition1 then Buy next bar at market;"
           "Value1 = Value1 + 1; Condition1 = Value1 >= 3;"
           "if Condition1 and Value1[1] = 2 then Sell next bar at market;")
    out = run_el(src, BARS, NQ)
    rows = [dict(zip(out["columns"], row)) for row in out["rows"]]
    assert [r["value1"] for r in rows[:3]] == ["1", "2", "3"]
    assert [r["condition1"] for r in rows[:3]] == ["False", "False", "True"]
    assert len(out["trades"]) >= 1
    assert run_oracle(src, BARS, NQ)["sha256"] == run_oracle(
        "Vars: Value1(0), Condition1(false);\n" + src, BARS, NQ)["sha256"]
