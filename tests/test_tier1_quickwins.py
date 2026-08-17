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

"""test_tier1_quickwins.py — Tier-1 quick-win regressions (2026-07-11 report).

Five independent, small-surface fixes, one assertion group each:

  1. Unimplemented EL keyword raises at TRANSPILE time (not a runtime NameError),
     carrying the original keyword spelling and the EL source line.
  2. The `total` order modifier survives EL -> Python -> EL two-way
     (reverse.py_to_el(reverse.el_to_py(src))).
  3. Booleans emit lowercase `true`/`false`.
  4. A literal quantity of 1 emits the singular noun `1 contract`.
  5. Leading blank lines at the top of a source are preserved on round trip.

Exit 0 only if all five groups pass; prints a one-line summary.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler.codegen import UnimplementedKeywordError
from pl_transpiler import reverse


def group_unimplemented_keyword():
    """A generic pl_* target with no builtin impl must raise at transpile time,
    with the keyword name and line; and a KNOWN keyword must still transpile."""
    src = "Value1 = 3;\nValue2 = TotallyMadeUpFunc(Close, 3);\n"
    try:
        transpile(src, trace=True)
    except UnimplementedKeywordError as e:
        msg = str(e)
        assert "totallymadeupfunc" in msg, f"keyword spelling missing: {msg!r}"
        assert "line 2" in msg, f"EL line missing/wrong: {msg!r}"
    else:
        raise AssertionError("unimplemented keyword did not raise at transpile time")
    # A known keyword still transpiles cleanly (no false positive).
    assert transpile("Value1 = Average(Close, 10);\n", trace=True)


def group_total_two_way():
    """`... N contracts total ...` survives EL -> Python -> EL."""
    src = 'Sell ("x") 2 contracts total next bar at market;\n'
    py = reverse.el_to_py(src)
    assert "'total'" in py, f"total dropped from forward codegen tuple:\n{py}"
    el = reverse.py_to_el(py)
    assert "total" in el.lower(), f"total lost in reverse emit: {el!r}"
    # And the direct AST round trip keeps it too.
    assert "total" in reverse.el_roundtrip(src).lower()


def group_boolean_lowercase():
    """Booleans emit lowercase true/false."""
    out = reverse.el_roundtrip("Value1 = True;\nCondition1 = False;\n")
    assert "true" in out and "True" not in out, out
    assert "false" in out and "False" not in out, out


def group_singular_contract():
    """A literal quantity of 1 emits singular '1 contract'; !=1 stays plural."""
    one = reverse.el_roundtrip("Buy 1 contract next bar at market;\n")
    assert "1 contract " in one and "1 contracts" not in one, one
    two = reverse.el_roundtrip("Buy 2 contracts next bar at market;\n")
    assert "2 contracts" in two, two


def group_leading_blanks():
    """Leading blank lines at the top of the source are reproduced, and the
    result is a re-emission fixed point (Rcyc-safe)."""
    src = "\n\nValue1 = 5;\n"
    out = reverse.el_roundtrip(src)
    assert out.startswith("\n\n"), repr(out)
    assert reverse.el_roundtrip(out) == out, "leading-blank emit is not a fixed point"
    # No spurious blanks when the source starts on line 1.
    assert not reverse.el_roundtrip("Value1 = 5;\n").startswith("\n")


GROUPS = [
    group_unimplemented_keyword,
    group_total_two_way,
    group_boolean_lowercase,
    group_singular_contract,
    group_leading_blanks,
]


def main():
    passed = 0
    for g in GROUPS:
        g()
        passed += 1
        print(f"  ok: {g.__name__}")
    print(f"TIER1 QUICKWINS: {passed}/{len(GROUPS)} groups pass")
    return 0 if passed == len(GROUPS) else 1


if __name__ == "__main__":
    sys.exit(main())
