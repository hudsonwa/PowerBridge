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

"""Native-vs-Python parity for numeric operators and builtins.

The Python path is the reference. Each case stores two operands in variables
(so nothing is folded at compile time), computes one expression, and trades on
how the result compares with a per-bar threshold, so the fills digest changes
if the value differs anywhere on the grid. Operands cover negatives, zero and
fractions.

For every case:

* Python raises (a zero divisor, ...) -> the kernel must refuse, never trade.
* Python runs -> the kernel's fills must match exactly. A kernel refusal is
  allowed only for builtins the kernel does not implement (they fall back).

Skips cleanly without the native extension.
"""
import itertools
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.conformance_harness import generate_tape  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    accel_available,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
BARS = generate_tape(7)

# Operands exactly representable in f32, so variable storage keeps them.
A_VALUES = ["-3", "-2.5", "-1", "-0.75", "0", "0.5", "2", "3.25"]
B_VALUES = ["-4", "-1.5", "-1", "0", "1", "2.5", "4"]

# Expressions the kernel implements: it must run them, not refuse.
NATIVE_BINARY = ["A mod B", "Mod(A, B)", "A / B", "MaxList(A, B)", "MinList(A, B)"]
NATIVE_UNARY = ["IntPortion(A)", "Sign(A)", "AbsValue(A)", "Square(A)", "-A mod 4"]
# Builtins the kernel does not implement: a refusal (Python fallback) is fine.
FALLBACK_UNARY = ["FracPortion(A)", "Round(A, 0)", "Floor(A)", "Ceiling(A)"]
FALLBACK_BINARY = ["Power(A, B)"]


def _program(a, b, expr):
    return (
        "Vars: A(0), B(0), R(0), T(0);\n"
        f"A = {a};\nB = {b};\nR = {expr};\n"
        "T = (BarNumber - 10) * 0.25;\n"
        "If R > T Then Buy next bar at market;\n"
        "If R < T Then SellShort next bar at market;\n"
        "If R = T Then Sell next bar at market;\n"
    )


def _cases():
    for expr in NATIVE_BINARY:
        for a, b in itertools.product(A_VALUES, B_VALUES):
            yield a, b, expr, True
    for expr in NATIVE_UNARY:
        for a in A_VALUES:
            yield a, "1", expr, True
    for expr in FALLBACK_BINARY:
        for a, b in itertools.product(A_VALUES, B_VALUES):
            yield a, b, expr, False
    for expr in FALLBACK_UNARY:
        for a in A_VALUES:
            yield a, "1", expr, False


CASES = list(_cases())


def test_numeric_grid_matches_python():
    mismatches = []
    for a, b, expr, native_expected in CASES:
        src = _program(a, b, expr)
        try:
            want = run_oracle(src, BARS, NQ)["sha256"]
        except Exception as exc:  # the reference raised
            want = None
            want_err = type(exc).__name__
        try:
            got = fills_sha256(run_native(src, BARS, NQ))
        except AccelRefused:
            got = None
        label = f"{expr} with A={a}, B={b}"
        if want is None:
            if got is not None:
                mismatches.append(f"{label}: Python raised {want_err}, native traded")
        elif got is None:
            if native_expected:
                mismatches.append(f"{label}: native refused a supported expression")
        elif got != want:
            mismatches.append(f"{label}: fills differ")
    assert not mismatches, "\n".join(mismatches)


def test_negative_mod_follows_python_sign():
    # Mod(-3, 4) is 1 on the Python path (C fmod would give -3).
    src = (
        "Vars: C(0), U(0);\nC = C - 3;\nU = Mod(C, 4);\n"
        "If U = 1 Then Buy next bar at market;\n"
        "If U = 3 Then SellShort next bar at market;\n"
    )
    want = run_oracle(src, BARS, NQ)
    assert want["fills"] > 0
    assert fills_sha256(run_native(src, BARS, NQ)) == want["sha256"]


def test_zero_divisor_on_an_untraded_bar_refuses():
    # Python raises on the first bar; the kernel must not skip past it.
    src = (
        "Vars: C(0), U(0);\nU = Close / C;\n"
        f"If Date >= {BARS[-1]['date']} Then Buy next bar at market;\n"
    )
    with pytest.raises(ZeroDivisionError):
        run_oracle(src, BARS, NQ)
    with pytest.raises(AccelRefused):
        run_native(src, BARS, NQ)


def test_zero_input_divisor_on_an_untraded_bar_refuses():
    # A non-zero input divisor keeps the prefix skip; a zero one must not.
    src = (
        "Inputs: Len(0);\nVars: U(0);\nU = Close / Len;\n"
        f"If Date >= {BARS[-1]['date']} Then Buy next bar at market;\n"
    )
    with pytest.raises(ZeroDivisionError):
        run_oracle(src, BARS, NQ)
    with pytest.raises(AccelRefused):
        run_native(src, BARS, NQ)
    nonzero = src.replace("Len(0)", "Len(20)")
    want = run_oracle(nonzero, BARS, NQ)["sha256"]
    assert fills_sha256(run_native(nonzero, BARS, NQ)) == want


def test_int_portion_of_an_overflowed_variable_refuses():
    # C = C * C overflows to inf; IntPortion(inf) raises in Python.
    src = (
        "Vars: C(2), U(0);\nC = C * C;\nU = IntPortion(C);\n"
        f"If Date >= {BARS[-1]['date']} Then Buy next bar at market;\n"
    )
    with pytest.raises(OverflowError):
        run_oracle(src, BARS, NQ)
    with pytest.raises(AccelRefused):
        run_native(src, BARS, NQ)


def test_int_portion_of_bar_data_keeps_the_skip():
    src = (
        "Vars: U(0);\nU = IntPortion(Time / 100);\n"
        f"If Date >= {BARS[-1]['date']} And U > 0 Then Buy next bar at market;\n"
    )
    want = run_oracle(src, BARS, NQ)["sha256"]
    assert fills_sha256(run_native(src, BARS, NQ)) == want
