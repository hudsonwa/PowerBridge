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

"""
test_fold_literal_f32.py — issue #15: fold literal-argument f32() in persist/trace.

persist/trace emit a bit-identical constant for `X = <number>` instead of
`f32(<number>)` every bar. History / series / expression wraps stay as
runtime `f32(...)`. The clean mirror dialect is unchanged (`Name = f32(<lit>)`);
DIALECT_VERSION stays 3.

Stdlib only, no captures.
"""
import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler import py_front
from pl_transpiler.codegen import DIALECT_VERSION
from pl_transpiler.runtime.pl_runtime import f32

RESET_EL = (
    "Vars: BestCloseSinceSignal(0), TrailStop(0);\n"
    "BestCloseSinceSignal = 0;\n"
    "TrailStop = 0;\n"
    "If Close > BestCloseSinceSignal Then BestCloseSinceSignal = Close;\n"
)

FRAC_EL = (
    "Vars: X(0);\n"
    "X = 0.1;\n"
)

NEG_EL = (
    "Vars: X(0);\n"
    "X = -1;\n"
)

EXPR_EL = (
    "Vars: X(0);\n"
    "X = Close + 1;\n"
)


def _same_float(a, b):
    return type(a) is float and type(b) is float and struct.pack("d", a) == struct.pack("d", b)


def test_persist_folds_literal_zero():
    persist = transpile(RESET_EL, persist=True)
    traced = transpile(RESET_EL, trace=True)
    for label, py in (("persist", persist), ("trace", traced)):
        assert "bestclosesincesignal = 0.0" in py, label
        assert "trailstop = 0.0" in py, label
        # The per-bar user-variable resets must not be an f32() round-trip.
        # NOTE: bare `f32(0)` still appears in the once-only __pb_slots seeding
        # on this base (rows 12-14) — a different, non-per-bar code path — so
        # assert on the assignment itself rather than banning the substring.
        assert "bestclosesincesignal = f32(0)" not in py, label
        assert "trailstop = f32(0)" not in py, label
        # History/state for these vars travels through the packed _pb_slots
        # tuple on this base, not a per-variable _state dict.
        assert "_pb_slots[:] = (bestclosesincesignal, trailstop)" in py, label
        # Close is hoisted to the per-bar _close (row 14) and the wrap-skip
        # keeps a single f32(), so the historical double wrap is gone.
        assert "f32(f32(close[-1]))" not in py, label
        assert "f32(_close)" not in py, label
    print("  [ok] persist/trace fold X=0 to 0.0; no per-bar f32() round-trip")


def test_clean_dialect_keeps_f32_literal():
    clean = transpile(RESET_EL, trace=False)
    explicit = transpile(RESET_EL, trace=False, persist=False)
    assert clean == explicit
    assert "bestclosesincesignal = f32(0)" in clean
    assert "trailstop = f32(0)" in clean
    assert "bestclosesincesignal = 0.0" not in clean
    ast = py_front.py_to_ast(clean)
    assert isinstance(ast, dict) and ast.get("type")
    print("  [ok] clean dialect still emits f32(0); py_front accepts it")


def test_folded_fraction_is_bit_identical():
    persist = transpile(FRAC_EL, persist=True)
    want = repr(f32(0.1))
    assert f"x = {want}" in persist, persist
    assert "f32(0.1)" not in persist
    got = float(want)
    assert _same_float(got, f32(0.1)), (got, f32(0.1))
    # Must not be the binary64 0.1 (that would skip EL single-precision).
    assert not _same_float(got, 0.1)
    print(f"  [ok] X=0.1 folds to {want} (bit-identical f32, not 0.1)")


def test_folded_negative_literal():
    persist = transpile(NEG_EL, persist=True)
    assert "x = -1.0" in persist, persist
    assert "f32(-1)" not in persist
    assert "f32((-1))" not in persist
    assert _same_float(float("-1.0"), f32(-1))
    print("  [ok] X=-1 folds to -1.0")


def test_expression_wrap_untouched():
    persist = transpile(EXPR_EL, persist=True)
    # On this base Close is hoisted to the per-bar `_close` (row 14), so the
    # expression wraps the hoisted name instead of a fresh close[-1] lookup.
    # The point of the assertion is unchanged: a non-literal RHS still gets
    # wrapped in f32().
    assert "x = f32((_close + 1))" in persist, persist
    print("  [ok] X = Close + 1 still wraps the expression in f32()")


def test_dialect_version_unchanged():
    assert DIALECT_VERSION == 3
    print("  [ok] DIALECT_VERSION stays 3")


def main():
    tests = [
        test_persist_folds_literal_zero,
        test_clean_dialect_keeps_f32_literal,
        test_folded_fraction_is_bit_identical,
        test_folded_negative_literal,
        test_expression_wrap_untouched,
        test_dialect_version_unchanged,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL FOLD-LITERAL-F32 TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
