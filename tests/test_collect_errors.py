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

"""test_collect_errors.py — collect-all transpile-time diagnostics (FL1).

One transpile() walks the ENTIRE program and reports EVERY unimplemented keyword
at once (name + source line each), then fails loud with no Python output. Proves:

  1. All unimplemented keywords across the program are reported in ONE transpile()
     call, each with its correct source line (incl. one inside an If-Then, one
     bar-offset call `F(...)[n]`, and one keyword repeated on two different lines).
  2. The `errors` attribute obeys the dedup (by name+line) / sort (by line then
     name, None lines last) contract.
  3. The N>1 message uses the multi-keyword report format.
  4. A single unknown keyword keeps the legacy byte-compatible message format.
  5. A fully-valid source still transpiles cleanly (no false positives).

Exit 0 only if all groups pass; prints a one-line summary. Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler.codegen import UnimplementedKeywordError


# Six unimplemented keyword occurrences over known lines:
#   line 1: FakeAlpha
#   line 2: FakeBeta twice (dedup collapses to a SINGLE (fakebeta, 2) entry)
#   line 3: FakeGamma inside an If-Then
#   line 4: FakeDelta as a bar-offset call  F(...)[2]
#   line 5: FakeEpsilon
#   line 6: FakeAlpha again (same name, DIFFERENT line -> kept as its own entry)
FIXTURE = (
    "Value1 = FakeAlpha(Close, 5);\n"
    "Value2 = FakeBeta(High, 3) + FakeBeta(Low, 4);\n"
    "If Close > 0 Then Value3 = FakeGamma(Low, 2);\n"
    "Value4 = FakeDelta(Close, 3)[2];\n"
    "Value5 = FakeEpsilon(Open, 1);\n"
    "Value6 = FakeAlpha(Volume, 2);\n"
)

EXPECTED = [
    {'name': 'fakealpha',   'line': 1},
    {'name': 'fakebeta',    'line': 2},
    {'name': 'fakegamma',   'line': 3},
    {'name': 'fakedelta',   'line': 4},
    {'name': 'fakeepsilon', 'line': 5},
    {'name': 'fakealpha',   'line': 6},
]


def _collect(src):
    """Transpile `src` expecting failure; return the raised exception."""
    try:
        transpile(src, trace=True)
    except UnimplementedKeywordError as e:
        return e
    raise AssertionError("expected UnimplementedKeywordError, none raised")


def group_all_reported_one_pass():
    """Every unimplemented keyword is reported in ONE transpile() call, each with
    its correct source line; the dedup/sort contract holds exactly."""
    e = _collect(FIXTURE)
    assert e.errors == EXPECTED, f"errors mismatch:\n{e.errors!r}\n!=\n{EXPECTED!r}"
    # The bar-offset and If-Then constructs each contributed their line.
    lines = {(d['name'], d['line']) for d in e.errors}
    assert ('fakedelta', 4) in lines, "bar-offset call line not captured"
    assert ('fakegamma', 3) in lines, "If-Then call line not captured"
    # FakeAlpha kept on both distinct lines; FakeBeta collapsed to one.
    assert lines >= {('fakealpha', 1), ('fakealpha', 6)}, "repeated-name/line dropped"
    assert sum(1 for d in e.errors if d['name'] == 'fakebeta') == 1, "dedup failed"


def group_sort_contract_with_none():
    """Sort is by (line, name) with None lines LAST (not raised in FIXTURE, so
    exercise it directly against the documented ordering)."""
    e = _collect(FIXTURE)
    lines_seq = [d['line'] for d in e.errors]
    # Non-None lines are ascending; the fixture has none-None here.
    non_none = [ln for ln in lines_seq if ln is not None]
    assert non_none == sorted(non_none), f"lines not sorted: {lines_seq}"
    assert all(ln is not None for ln in lines_seq), "unexpected None line in fixture"


def group_multi_message_format():
    """N>1 message uses the multi-keyword report format, in sorted order."""
    e = _collect(FIXTURE)
    msg = str(e)
    expected = (
        "cannot transpile: 6 unimplemented keywords: "
        "'fakealpha' at line 1; 'fakebeta' at line 2; 'fakegamma' at line 3; "
        "'fakedelta' at line 4; 'fakeepsilon' at line 5; 'fakealpha' at line 6"
    )
    assert msg == expected, f"multi message mismatch:\n{msg!r}\n!=\n{expected!r}"


def group_single_message_legacy():
    """A single unknown keyword keeps the legacy byte-compatible message + a
    one-element errors list."""
    e = _collect("Value1 = 3;\nValue2 = TotallyMadeUpFunc(Close, 3);\n")
    assert str(e) == "unimplemented EL keyword 'totallymadeupfunc' at line 2", repr(str(e))
    assert e.errors == [{'name': 'totallymadeupfunc', 'line': 2}], repr(e.errors)


def group_valid_still_transpiles():
    """A fully-valid source still transpiles cleanly (no false positive)."""
    py = transpile("Value1 = Average(Close, 10);\nValue2 = Value1 + 1;\n", trace=True)
    assert py and "def strategy" in py, "valid source failed to transpile"


def group_bare_marker_keyword_reported():
    """F2: a bare BUILTIN_FUNC_MAP keyword whose internal `__marker__` py_name has
    no faithful zero-arg expansion (e.g. `FastD`) must be COLLECTED as unimplemented
    under its EL name — never emitted as an undefined `__fastd__()` marker call."""
    e = _collect("Value1 = FastD;\n")
    assert e.errors == [{"name": "fastd", "line": 1}], repr(e.errors)
    # partial output routes it to a stub (not the misleading N==0 'identical' header).
    code = transpile("Value1 = FastD;\n", trace=True, partial=True)
    assert "__fastd__" not in code, f"marker leaked into partial output:\n{code}"
    assert "pl_partial_stub('fastd'" in code, code
    assert "0 unimplemented constructs" not in code, "N==0 header stamped on stubbed code"


def group_bare_unimpl_ident_reported():
    """F3: a bare unimplemented keyword used as a value (`AvgWinTrade`) is collected
    (not emitted as a raw lowercased identifier that NameErrors at runtime), and a
    program mixing a bare unknown with a call-form unknown reports BOTH."""
    e = _collect("Value1 = AvgWinTrade;\n")
    assert e.errors == [{"name": "avgwintrade", "line": 1}], repr(e.errors)

    # Bare + call-form unknowns coexisting: the report must contain both.
    e2 = _collect("Value1 = BareKwM + RealFake(Close);\n")
    names = {d["name"] for d in e2.errors}
    assert names == {"barekwm", "realfake"}, repr(e2.errors)


def group_declared_and_assigned_names_not_flagged():
    """F3 must NOT false-positive: declared vars (incl. NESTED Variables blocks at
    any depth) and names defined purely by assignment stay valid — only names that
    are READ but never declared and never assigned are unimplemented."""
    # Nested Variables: block inside a Begin/End — declared, must transpile clean.
    nested = (
        "If Close > 0 Then Begin\n"
        "    Variables: NestedV(0), NestedS(\"\");\n"
        "    NestedV = Close + 1;\n"
        "    NestedS = \"x\";\n"
        "End;\n"
        "Value1 = NestedV;\n"
    )
    py = transpile(nested, trace=True)
    assert "def strategy" in py, "nested-declared vars wrongly flagged"
    # A name defined only by a plain assignment (never declared) is a valid local.
    py2 = transpile("AssignedOnly = Close;\nValue1 = AssignedOnly + 1;\n", trace=True)
    assert "def strategy" in py2, "assignment-defined local wrongly flagged"


def group_nested_declared_shadows_builtin():
    """FL5 RC2: a name declared in a NESTED Variables/Inputs/Arrays block that collides
    with a builtin must resolve to the DECLARED local — never be mis-flagged as
    unimplemented (a __marker__ builtin like FastD), nor emit the builtin expansion and
    discard the declaration (a bare-computed reserved word like Range, or an indicator
    like CCI). The identical declaration at top level already transpiles fine."""
    nested = (
        "If Close > 0 Then Begin\n"
        "    Variables: {name}(0);\n"
        "    {name} = {rhs};\n"
        "End;\n"
        "Value1 = {name};\n"
    )
    for name, rhs in [("FastD", "5"), ("Range", "7"), ("CCI", "7"), ("Floor", "3")]:
        py = transpile(nested.format(name=name, rhs=rhs), trace=True)
        assert "def strategy" in py, f"nested-declared {name} wrongly flagged"
        compile(py, "<f>", "exec")  # the declared local must not collide/SyntaxError
    # Bare-computed Range must NOT be expanded when it is a declared local.
    py_range = transpile(nested.format(name="Range", rhs="7"), trace=True)
    assert "(high[-1] - low[-1])" not in py_range, \
        "declared Range wrongly emitted the High-Low bare-computed expansion"
    # PRESERVE the correct behavior: an UNDECLARED bare builtin still expands.
    py_undecl = transpile("Value1 = Range;\n", trace=True)
    assert "(high[-1] - low[-1])" in py_undecl, \
        "undeclared bare Range no longer resolves to its builtin expansion"


def group_lvalue_unimpl_keyword_collected():
    """FL5 RC3: assignment to a recognized-but-unimplemented EL keyword (read-only in
    EL) must COLLECT+RAISE in strict — a WRITE must not silently bypass the fail-loud
    that a READ of the same keyword already triggers. Genuine assigned-AND-read locals
    and runtime internals that surface write-only in round-tripped EL stay valid."""
    e_write = _collect("AvgWinTrade = Close;\n")
    assert e_write.errors == [{"name": "avgwintrade", "line": 1}], repr(e_write.errors)
    # The read of the same keyword already raised — the write now matches that contract.
    e_read = _collect("Value1 = AvgWinTrade;\n")
    assert e_read.errors == [{"name": "avgwintrade", "line": 1}], repr(e_read.errors)
    # A genuine local assigned AND read stays valid (not flagged).
    py = transpile("AssignedOnly = Close;\nValue1 = AssignedOnly + 1;\n", trace=True)
    assert "def strategy" in py, "assigned+read local wrongly flagged"
    # A runtime internal that surfaces write-only in round-tripped EL stays valid.
    py2 = transpile("_commentary = Close;\n", trace=True)
    assert "def strategy" in py2, "runtime internal _commentary wrongly flagged"


def group_assigned_and_read_keyword_not_rescued():
    """FL6 BUG C: the "assigned & read" lvalue rescue must not whitelist a
    recognized-but-unimplemented keyword nor a name that is READ before it is ever
    cleanly defined / is only self-referential — otherwise its bare read silently
    bypasses the strict fail-loud and drops from the collect-all report."""
    # A builtin-marker keyword read AND written (FastD) is not a local declaration —
    # both the read and the illegal write to the builtin must be collected.
    e_fastd = _collect("Value1 = FastD;\nFastD = 3;\n")
    names = {x["name"] for x in e_fastd.errors}
    assert names == {"fastd"}, repr(e_fastd.errors)
    assert any(x["line"] == 1 for x in e_fastd.errors), repr(e_fastd.errors)
    # Read-before-write: FakeA read at line 1 (before its write at line 2) must be
    # collected ALONGSIDE the read-only FakeB — the report must be COMPLETE.
    e_ab = _collect("Value1 = FakeA;\nFakeA = 2;\nValue2 = FakeB;\n")
    names_ab = {x["name"] for x in e_ab.errors}
    assert names_ab == {"fakea", "fakeb"}, repr(e_ab.errors)
    assert {"name": "fakea", "line": 1} in e_ab.errors, repr(e_ab.errors)
    assert {"name": "fakeb", "line": 3} in e_ab.errors, repr(e_ab.errors)
    # Self-referential-only write establishes no independent value -> collected.
    e_self = _collect("AvgWinTrade = AvgWinTrade + 1;\n")
    assert {x["name"] for x in e_self.errors} == {"avgwintrade"}, repr(e_self.errors)
    # PRESERVE: a genuine local cleanly defined BEFORE its reads (the flattened
    # nested-Variables shape the reverse path emits) stays valid.
    py = transpile("nested = 0;\nnested = nested + 1;\nValue1 = nested;\n", trace=True)
    assert "def strategy" in py, "clean-defined assign local wrongly flagged"


def group_reserved_ident_no_binding_reported():
    """FL6 BUG D: a bare reserved identifier the parser recognizes but which has NO
    pl_ target and NO runtime binding (e.g. Pi) would emit a raw undefined name ->
    NameError; it must instead COLLECT+RAISE like any unimplemented keyword. A
    reserved word that DOES bind at runtime (NewLine) stays valid."""
    e_pi = _collect("Value1 = Pi;\n")
    assert e_pi.errors == [{"name": "pi", "line": 1}], repr(e_pi.errors)
    # A reserved word that resolves via the runtime import stays valid (no leak the
    # other way): NewLine binds, so it is NOT flagged.
    py = transpile("Value1 = NewLine;\n", trace=True)
    assert "def strategy" in py, "runtime-bound reserved word NewLine wrongly flagged"
    # MarketPosition resolves to the prologue local _market_position — still valid.
    py2 = transpile("Value1 = MarketPosition;\n", trace=True)
    assert "def strategy" in py2, "MarketPosition wrongly flagged"


def group_bare_python_builtin_collision_reported():
    """Audit a2-F1: a bare EL identifier that COLLIDES with a Python builtin name
    (`all`, `any`, `sum`, `map`, `id`, `input`, ...) must NOT be trusted as a valid
    runtime binding just because `dir(builtins)` contains it. Emitting it raw would
    bind it to the Python builtin object (`Value1 = all;` -> `value1 = all`, the
    function) — a wrong value / stray runtime TypeError instead of the contracted
    fail-loud. It must COLLECT+RAISE like any unimplemented keyword (parity with Pi)."""
    for kw in ("all", "any", "sum", "map"):
        e = _collect(f"Value1 = {kw};\n")
        assert e.errors == [{"name": kw, "line": 1}], (kw, repr(e.errors))
    # NewLine still resolves via a REAL pl_runtime binding (not a Python builtin) —
    # the narrowing must not flag genuine runtime-bound reserved words.
    py = transpile("Value1 = NewLine;\n", trace=True)
    assert "def strategy" in py, "runtime-bound reserved word NewLine wrongly flagged"


GROUPS = [
    group_all_reported_one_pass,
    group_sort_contract_with_none,
    group_multi_message_format,
    group_single_message_legacy,
    group_valid_still_transpiles,
    group_bare_marker_keyword_reported,
    group_bare_unimpl_ident_reported,
    group_declared_and_assigned_names_not_flagged,
    group_nested_declared_shadows_builtin,
    group_lvalue_unimpl_keyword_collected,
    group_assigned_and_read_keyword_not_rescued,
    group_reserved_ident_no_binding_reported,
    group_bare_python_builtin_collision_reported,
]


def main():
    passed = 0
    for g in GROUPS:
        g()
        passed += 1
        print(f"  ok: {g.__name__}")
    print(f"COLLECT ERRORS: {passed}/{len(GROUPS)} groups pass")
    return 0 if passed == len(GROUPS) else 1


if __name__ == "__main__":
    sys.exit(main())
