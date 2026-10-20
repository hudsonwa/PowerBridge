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
test_lexer_failloud.py — the lexer must FAIL LOUD on characters that are not
part of EasyLanguage, never silently drop them.

Silently skipping unknown characters would let garbage transpile to
plausible-but-wrong EL, contradicting the project's absolute fail-loud
invariant. This test asserts:

  (1) an unknown character (`@`) raises PLSyntaxError whose message names the
      offending character and its 1-based line, and renders a caret;
  (2) a stray unmatched `}` also raises;
  (3) a set of representative VALID snippets (operators, strings, numbers,
      comments incl. nested braces, `crosses above`) still tokenise unchanged.

Capture-independent and portable (stdlib only). Exit 0 iff every assertion
holds.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.lexer import tokenise, PLSyntaxError


def test_unknown_char_raises():
    """`a @@@ b` raises PLSyntaxError naming the char, line 1, with a caret."""
    try:
        tokenise("a @@@ b")
    except PLSyntaxError as e:
        msg = str(e)
        assert "@" in msg, f"message does not name the offending char: {msg!r}"
        assert "line 1" in msg, f"message does not locate line 1: {msg!r}"
        assert "^" in msg, f"message has no caret: {msg!r}"
        print("  [ok] unknown '@' raises PLSyntaxError with char+line+caret")
        return
    raise AssertionError("tokenise('a @@@ b') did NOT raise — unknown char was skipped")


def test_stray_close_brace_raises():
    """A stray unmatched `}` reaches the unknown branch and must raise."""
    try:
        tokenise("a } b")
    except PLSyntaxError as e:
        assert "}" in str(e), f"message does not name the stray brace: {str(e)!r}"
        print("  [ok] stray '}' raises PLSyntaxError")
        return
    raise AssertionError("tokenise('a } b') did NOT raise on the stray '}'")


def test_valid_snippets_unchanged():
    """Representative valid EL tokenises without error."""
    snippets = [
        "value1 := close + 1;",
        "if x >= y then buy next bar;",
        'Print("hello, world");',
        "value2 = 3.14159 * high - low;",
        "value3 = openD(0) mod 2;",
        "{ outer { nested } comment } x = 1;",
        "// line comment\nvalue4 = 1;",
        "if fast crosses above slow then buy next bar at market;",
        "condition1 = a <> b and c <= d;",
    ]
    for s in snippets:
        toks, positions = tokenise(s)
        assert len(toks) == len(positions), f"token/position length mismatch for {s!r}"
        assert toks, f"no tokens produced for {s!r}"
    print(f"  [ok] {len(snippets)} valid snippets tokenise cleanly")


def main():
    tests = [
        test_unknown_char_raises,
        test_stray_close_brace_raises,
        test_valid_snippets_unchanged,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL LEXER FAIL-LOUD TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
