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

"""Pass 4 of mc_ground_check: argument types checked against the catalog's arg_types."""
import copy

from pl_transpiler.tools import el_emit, mc_ground_check as gc

HEADER = 'Inputs: Len(10);\nVars: Sym(""), Flag(false), Num(0), X(0);\n'


def _bad(body, sigs=None):
    return gc.argtype_violations(HEADER + body, sigs if sigs is not None else gc.load_signatures())


def test_string_literal_where_numeric_declared():
    assert _bad('X = Round(Close, "two");\n') == [('Round', 2, 'numeric', 'string', 3)]


def test_string_and_bool_variables_and_literals():
    got = _bad('X = Power(Sym, 2);\nX = AbsValue(Flag) + SquareRoot(true);\n')
    assert got == [('Power', 1, 'numeric', 'string', 3),
                   ('AbsValue', 1, 'numeric', 'bool', 4),
                   ('SquareRoot', 1, 'numeric', 'bool', 4)]


def test_numeric_and_uncertain_arguments_accepted():
    body = ('X = Power(Close, Len) + Round(Num * 2, 0) + AbsValue(Average(Close, 10));\n'
            'X = Power(Sym + "", 2);\n'  # an expression: its type is not inferred, so not checked
            'Plot1(X, "Power(1)");\n'     # a call inside a string literal is not a call
            '{ Power("x", 2) in a comment }\n')
    assert _bad(body) == []


def test_any_and_untyped_parameters_skipped():
    # Plot1's catalog arg types are unknown/any: a string there is never rejected.
    assert _bad('Plot1("text");\nValue1 = Text_New(Date, Time, Close, "a");\n') == []


def test_documented_unverified_records_are_skipped():
    sigs = copy.deepcopy(gc.load_signatures())
    sigs['round']['verification'] = 'documented-unverified'
    assert _bad('X = Round(Close, "two");\n', sigs) == []


def test_user_declared_name_shadowing_a_builtin_skipped():
    assert gc.argtype_violations('Vars: Round(0);\nValue1 = Round("a");\n', gc.load_signatures()) == []


def test_el_emit_rejects_and_names_function_position_types_line(tmp_path, capsys):
    src = tmp_path / "bad.el"
    src.write_text('Vars: X(0);\nX = Power(Close, "2");\nPlot1(X);\n')
    assert el_emit.main([str(src)]) == 1
    out = capsys.readouterr().out
    assert "1 ARG-TYPE-BAD" in out
    assert "line 2: Power argument 2: expected numeric, found string" in out


def test_el_emit_accepts_matching_types(tmp_path, capsys):
    src = tmp_path / "good.el"
    src.write_text('Vars: X(0);\nX = Power(Close, 2) + Round(X, 2);\nPlot1(X);\n')
    assert el_emit.main([str(src)]) == 0
    assert "0 ARG-TYPE-BAD" in capsys.readouterr().out
