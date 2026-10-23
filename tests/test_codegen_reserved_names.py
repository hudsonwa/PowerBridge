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

"""EL names that are Python built-ins, keywords or runtime helpers (#130).

Codegen renames a colliding user identifier (``max`` -> ``_el_max``) so the
generated module keeps its built-ins. A program must behave exactly like the
same program with a harmless name: same fills, and trace columns and input
keys keep the EL name. A block holding only a redeclaration emits ``pass``.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.codegen_util import (  # noqa: E402
    _AST_BUILTIN_IDENTS,
    reserved_python_names,
)
from pl_transpiler.lexer import PLSyntaxError  # noqa: E402
from pl_transpiler.reverse import py_to_el  # noqa: E402
from pl_transpiler.runtime.accel import accel_available, run_native  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")


def program(n):
    """`n` as a variable read by lookback and as a series argument; K is
    redeclared inside a block (the block emits no other code)."""
    return (
        f"Vars: {n}(0), K(0);\n"
        f"{n} = Close - Close[2];\n"
        f"K = {n}[1] + Average({n}, 3);\n"
        "If Close > Open Then Begin\n"
        "    Vars: K(0);\n"
        "End;\n"
        f"If {n} > K Then Buy next bar at market Else Sell next bar at market;\n"
    )


def roles(n):
    """`n` as an input, an array and a for-loop variable."""
    return (
        f"Inputs: {n}(3);\n"
        f"Vars: Acc(0), {n}_i(0);\n"
        f"Arrays: {n}_a[3](0);\n"
        f"For {n}_i = 1 To {n} Begin\n"
        f"    {n}_a[{n}_i] = Close[{n}_i];\n"
        "End;\n"
        f"Acc = {n}_a[1] - {n}_a[{n}];\n"
        "If Acc > 0 Then Buy next bar at market Else Sell next bar at market;\n"
    )


def _parses(n):
    try:
        transpile(program(n))
    except PLSyntaxError:  # an EL keyword (if, vars, true, ...) is no variable name
        return False
    return True


# Every reserved name that EL accepts as a variable name.
NAMES = sorted(
    n for n in reserved_python_names()
    if n.isidentifier() and not n.startswith("__")
    and n not in _AST_BUILTIN_IDENTS and _parses(n)
)
# A spread for the slower run checks: builtins, keywords, generated-module
# helpers and locals, runtime functions.
SAMPLE = [
    "abs", "max", "min", "int", "str", "float", "len", "sum", "round", "pow",
    "list", "type", "id", "any", "all", "iter", "object", "range",
    "def", "class", "lambda", "pass", "import", "global", "yield", "none",
    "f32", "el_round", "el_round0", "pl_file", "math", "strategy", "kwargs",
    "time_s", "_state", "_pb_slots", "_trace", "_el_max", "pl_average",
]
# Inputs are read from the engine's kwargs by EL name, so an input may not
# share a name with a key the engine itself passes.
ENGINE_KWARGS = {"time_s", "_state"}


@pytest.fixture(scope="module")
def bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


def test_reserved_set_is_derived():
    assert {"max", "lambda", "f32", "pl_average", "kwargs", "_state"} <= reserved_python_names()
    assert len(NAMES) > 500
    assert set(SAMPLE) - {"_el_max"} <= set(NAMES)


@pytest.mark.parametrize("dialect", ["runnable", "mirror"])
def test_every_reserved_name_compiles(dialect):
    for n in NAMES:
        compile(transpile(program(n), dialect=dialect), n, "exec")
        compile(transpile(roles(n), dialect=dialect), n, "exec")


def test_round_trip_restores_el_names():
    for n in NAMES:
        el = py_to_el(transpile(program(n), dialect="mirror")).lower()
        assert f"{n} = close - close[2]" in el, n
        assert f"vars: {n}( 0 )" in el and "vars: k( 0 )" in el, n
        # Arrays are outside the mirror dialect, so no roles() here.
        src = f"Inputs: {n}(3);\nVars: {n}_i(0);\nFor {n}_i = 1 To {n} Begin\nEnd;\n"
        el = py_to_el(transpile(src, dialect="mirror")).lower().replace(" ", "")
        assert f"inputs:{n}(3)" in el and f"for{n}_i=1to{n}begin" in el, n
        # A mangled Var must not end the declaration block early.
        el = py_to_el(transpile(f"Vars: {n}(0);\nArrays: q{n}[3](0);\nq{n}[1] = {n};\n",
                                dialect="mirror")).lower().replace(" ", "")
        assert f"vars:{n}(0)" in el and f"arrays:q{n}[3](0)" in el, n


@pytest.mark.parametrize("make", [program, roles])
@pytest.mark.parametrize("n", SAMPLE)
def test_runs_like_a_harmless_name(make, n, bars):
    if make is roles and n in ENGINE_KWARGS:
        pytest.skip("input name is an engine kwargs key")
    want = run_el(make("zq" + n), bars, NQ)
    got = run_el(make(n), bars, NQ)
    assert len(want["trades"]) > 10
    assert got["trades"] == want["trades"]
    # Same trace under the EL names (columns are sorted by name).
    renamed = [c.replace("zq", "", 1) for c in want["columns"]]
    assert sorted(got["columns"]) == sorted(renamed) and n in got["columns"]
    assert [dict(zip(got["columns"], r)) for r in got["rows"]] == [
        dict(zip(renamed, r)) for r in want["rows"]]


def test_input_key_keeps_el_name():
    assert "kwargs.get('max', 3)" in transpile(roles("max"))


@pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)
@pytest.mark.parametrize("make", [program, roles])
@pytest.mark.parametrize("n", SAMPLE)
def test_native_matches_python(make, n, bars):
    if make is roles and n in ENGINE_KWARGS:
        pytest.skip("input name is an engine kwargs key")
    source = make(n)
    assert fills_sha256(run_native(source, bars, NQ)) == run_oracle(source, bars, NQ)["sha256"]


@pytest.mark.parametrize("dialect", ["runnable", "mirror"])
def test_redeclaration_only_block_emits_pass(dialect):
    source = (
        "Vars: K(0);\n"
        "If Close > Open Then Begin\n"
        "    Vars: K(0);\n"
        "End Else Begin\n"
        "    Vars: K(0);\n"
        "End;\n"
        "While K > 1 Begin\n"
        "    Vars: K(0);\n"
        "End;\n"
        "For Value1 = 1 To 2 Begin\n"
        "    Vars: K(0);\n"
        "End;\n"
        "Switch (K) Begin\n"
        "    Case 1: Vars: K(0);\n"
        "    Default: Vars: K(0);\n"
        "End;\n"
    )
    compile(transpile(source, dialect=dialect), "<el>", "exec")
