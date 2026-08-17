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

"""test_partial_mode.py — opt-in --partial / partial= transpile mode (FL2).

With partial=True, an unimplemented EL keyword becomes an execution-time stub
that RAISES UnimplementedKeywordError when evaluated (never a wrong value), under
a NOT-FAITHFUL watermark. The strict default is unchanged. Proves:

  (a) A hit stub raises at EXECUTION under run_el(partial=True); the strict
      default raises at TRANSPILE time (transpile itself raises).
  (b) A stub behind a never-true guard never fires: run_el(partial=True) runs to
      completion over examples/NQ_sample_bars.csv and yields rows IDENTICAL to the
      same strategy with the unknown line removed (run strict).
  (c) The watermark contract for N>0 (header block naming every stub) and N==0
      (one ack line + byte-identical strict output, plain and trace).
  (d) The strict default transpile of the fixture still raises.

Exit 0 only if all groups pass. Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler.errors import UnimplementedKeywordError
from pl_transpiler.tools.pl_run import run_el, load_bars_csv
from pl_transpiler.runtime.instrument_config import get_config

BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

# Stub is HIT every bar (unconditional unknown assignment).
FIXTURE_HIT = (
    "Vars: MyOut(0);\n"
    "MyOut = FakeUnknownKw(Close);\n"
)

# Stub sits behind a never-true guard (NQ close ~15000, never < -999999).
FIXTURE_GUARDED = (
    "Vars: MyOut(0);\n"
    "MyOut = Close;\n"
    "If Close < -999999 Then MyOut = FakeUnknownKw(Close);\n"
)

# Same strategy with the unknown line removed (the faithful strict equivalent).
FIXTURE_REMOVED = (
    "Vars: MyOut(0);\n"
    "MyOut = Close;\n"
)

# Fully valid source (no unimplemented constructs).
FIXTURE_VALID = (
    "Value1 = Average(Close, 10);\n"
    "Value2 = Value1 + 1;\n"
)


def group_a_hit_stub_execution_vs_transpile():
    """A hit stub raises at EXECUTION under partial; strict raises at TRANSPILE."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")

    # partial transpile SUCCEEDS (produces code) — no transpile-time raise.
    code = transpile(FIXTURE_HIT, trace=True, partial=True)
    assert "pl_partial_stub" in code, "partial transpile did not emit a stub"

    # strict transpile RAISES (transpile time).
    try:
        transpile(FIXTURE_HIT, trace=True)
        raise AssertionError("strict transpile did not raise")
    except UnimplementedKeywordError:
        pass

    # run_el(partial=True) raises at EXECUTION (stub evaluated on the first bar).
    try:
        run_el(FIXTURE_HIT, bars, config, partial=True)
        raise AssertionError("run_el(partial=True) did not raise on hit stub")
    except UnimplementedKeywordError as e:
        assert "partial-mode stub executed" in str(e), repr(str(e))

    # Default run_el raises too (via the strict transpile, before any bar runs).
    try:
        run_el(FIXTURE_HIT, bars, config)
        raise AssertionError("default run_el did not raise")
    except UnimplementedKeywordError as e:
        assert "partial-mode stub executed" not in str(e), \
            "default path must fail at transpile time, not via a stub"


def group_b_guarded_stub_identical_rows():
    """A never-fired stub yields rows identical to the unknown-line-removed strict
    run over the sample bars."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")

    partial_out = run_el(FIXTURE_GUARDED, bars, config, partial=True)
    strict_out = run_el(FIXTURE_REMOVED, bars, config)

    assert partial_out["columns"] == strict_out["columns"], (
        f"columns differ:\n{partial_out['columns']!r}\n!=\n{strict_out['columns']!r}")
    assert partial_out["rows"] == strict_out["rows"], "per-bar rows differ"
    assert partial_out["trades"] == strict_out["trades"], "trades differ"
    assert partial_out["rows"], "no rows produced (sample bars did not run)"


def group_c_watermark_contract():
    """Watermark: N>0 header block names every stub; N==0 one ack line + identical
    strict output (plain and trace)."""
    # N>0.
    p = transpile(FIXTURE_GUARDED, trace=True, partial=True)
    lines = p.splitlines()
    assert lines[0].startswith("# ="), "watermark block must start the file"
    head = "\n".join(lines[:10])
    assert "PARTIAL TRANSPILE — NOT FAITHFUL" in head, "missing NOT-FAITHFUL phrase"
    assert "1 unimplemented construct(s)" in head, "missing/incorrect count N"
    assert "do not trust backtest results" in head, "missing do-not-trust sentence"
    assert "UnimplementedKeywordError" in head, "missing stub-raises note"
    assert "'fakeunknownkw' at line 3" in head, "stub not named with its line"

    # N==0, plain: exactly one ack line + byte-identical strict output.
    ack = ("# PARTIAL TRANSPILE requested — 0 unimplemented constructs; "
           "output identical to the strict transpile.\n")
    p0 = transpile(FIXTURE_VALID, partial=True)
    assert p0 == ack + transpile(FIXTURE_VALID), "N==0 plain not byte-identical"

    # N==0, trace variant.
    p0t = transpile(FIXTURE_VALID, trace=True, partial=True)
    assert p0t == ack + transpile(FIXTURE_VALID, trace=True), \
        "N==0 trace not byte-identical"


def group_d_default_transpile_still_raises():
    """The strict default transpile of the fixture still raises (all-or-nothing)."""
    try:
        transpile(FIXTURE_GUARDED)
        raise AssertionError("default transpile of unknown-bearing source did not raise")
    except UnimplementedKeywordError as e:
        assert e.errors and e.errors[0]["name"] == "fakeunknownkw", repr(e.errors)


# F1: a Vars: initializer with an unimplemented keyword. In TRACE mode the var is
# seeded from _state, so the stub for the unknown init keyword must live in the
# state seed — otherwise the var silently starts at ''/0 and the run COMPLETES
# with a wrong value instead of raising. Covers a plain call, a nested arithmetic
# form, and a string-typed var.
F1_VAR_INIT_CASES = [
    ("Vars: x(FakeF(Close));\nPlot1(x);\n", "fakef"),
    ("Vars: x(1 + FakeF(Close));\nPlot1(x);\n", "fakef"),
    ('Vars: s(FakeS("a"));\nPlot1(s);\n', "fakes"),
]


def group_e_var_init_stub_raises_not_wrong_value():
    """A var whose initializer contains an unimplemented keyword RAISES at execution
    under partial mode (never completes with a placeholder ''/0 value)."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")
    for src, kw in F1_VAR_INIT_CASES:
        # partial transpile emits a stub, and it is the STATE SEED (setdefault line).
        code = transpile(src, trace=True, partial=True)
        seed = [ln for ln in code.splitlines()
                if "setdefault" in ln and "pl_partial_stub" in ln]
        assert seed, f"F1 stub not in state seed for {src!r}:\n{code}"
        assert f"'{kw}'" in code, f"watermark did not name {kw} for {src!r}"
        # strict transpile raises at transpile time (all-or-nothing).
        try:
            transpile(src, trace=True)
            raise AssertionError(f"F1 strict transpile did not raise for {src!r}")
        except UnimplementedKeywordError:
            pass
        # run_el(partial=True) RAISES the moment the var initializes — no wrong value.
        try:
            run_el(src, bars, config, partial=True)
            raise AssertionError(f"F1 run_el(partial) did not raise for {src!r}")
        except UnimplementedKeywordError as e:
            assert "partial-mode stub executed" in str(e), repr(str(e))
            assert kw in str(e), f"raise did not name {kw}: {str(e)!r}"


def group_f_argless_stub_raises_unimplemented_not_typeerror():
    """F4: an emitted stub carries NO arguments, so a partial run raises
    UnimplementedKeywordError — never an unrelated TypeError from evaluating the
    original argument expressions before the stub fires. Nested unknowns are still
    both collected (watermark complete) and the run still fails loud."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")

    # Bar-offset call whose args (`f32(close[-1])[:-2], 3`) would TypeError if kept.
    src = "Value1 = FakeOff(Close, 3)[2];\n"
    code = transpile(src, trace=True, partial=True)
    assert "pl_partial_stub('fakeoff', 1)()" in code, f"stub not argless:\n{code}"
    try:
        run_el(src, bars, config, partial=True)
        raise AssertionError("F4 run_el(partial) did not raise")
    except UnimplementedKeywordError as e:
        assert "partial-mode stub executed" in str(e), repr(str(e))
    except TypeError as e:  # the exact bug F4 fixes
        raise AssertionError(f"F4 regression: TypeError not UnimplementedKeywordError: {e!r}")

    # Nested unknowns: BOTH named in the watermark, argless stub, still raises.
    nested = "Value1 = FakeOuter(FakeInner(Close));\n"
    ncode = transpile(nested, trace=True, partial=True)
    assert "pl_partial_stub('fakeouter', 1)()" in ncode, f"nested stub not argless:\n{ncode}"
    assert "'fakeouter'" in ncode and "'fakeinner'" in ncode, "nested watermark incomplete"
    try:
        transpile(nested, trace=True)
        raise AssertionError("nested strict transpile did not raise")
    except UnimplementedKeywordError as e:
        names = {d["name"] for d in e.errors}
        assert names == {"fakeouter", "fakeinner"}, repr(e.errors)


# FL5 RC1: an IMPLEMENTED enclosing keyword whose inline expansion DROPS, RELOCATES,
# or STRING-INTERPOLATES its argument text (CountIf keeps only args[1]; EntryName/
# ExitName/BaseDataNumber/CurrentDate/Recalculate ignore args; BarNumberOfData
# f-string-interpolates an arg) must not let a nested stub vanish or break the literal.
# Each case: partial output COMPILES and RAISES at execution naming the NESTED
# unimplemented keyword; the implemented enclosing keyword is never reported.
RC1_DROP_CASES = [
    ("Value1 = CountIf(FakeF(Close) > 0, 10);\n", "fakef", "countif"),
    ("Value1 = EntryName(FakeF(Close));\n", "fakef", "entryname"),
    ("Value1 = ExitName(FakeF(Close));\n", "fakef", "exitname"),
    ("Value1 = BarNumberOfData(FakeF(Close));\n", "fakef", "barnumberofdata"),
]


def group_g_stub_survives_arg_dropping_expansions():
    """FL5 RC1 (F1/F3): a nested unimplemented keyword inside an IMPLEMENTED call whose
    expansion drops/interpolates args collapses the WHOLE call to a standalone argless
    stub — the partial output COMPILES (no SyntaxError) and RAISES at execution (never
    completes with a wrong value), naming the nested keyword, not the enclosing one."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")
    for src, kw, enclosing in RC1_DROP_CASES:
        code = transpile(src, trace=True, partial=True)
        compile(code, "<f>", "exec")  # F3: must not be a SyntaxError
        assert f"pl_partial_stub('{kw}'" in code, f"stub not named {kw}:\n{code}"
        # F1: run RAISES (never completes with a real value); names the nested kw.
        try:
            run_el(src, bars, config, partial=True)
            raise AssertionError(f"partial run did not raise for {src!r}")
        except UnimplementedKeywordError as e:
            assert "partial-mode stub executed" in str(e), repr(str(e))
            assert kw in str(e), f"raise did not name {kw}: {str(e)!r}"
        # strict raises at transpile, naming the NESTED keyword — never the enclosing
        # IMPLEMENTED keyword (a user must not see an implemented keyword flagged).
        try:
            transpile(src, trace=True)
            raise AssertionError(f"strict did not raise for {src!r}")
        except UnimplementedKeywordError as e:
            names = {d["name"] for d in e.errors}
            assert kw in names, repr(e.errors)
            assert enclosing not in names, \
                f"implemented enclosing '{enclosing}' wrongly reported: {e.errors!r}"


def group_h_stub_target_is_standalone_statement():
    """FL5 RC1 (F2): an assignment whose TARGET is an unimplemented keyword emits the
    stub as a STANDALONE statement, never `pl_partial_stub(...)() = rhs` (a Python
    SyntaxError). Partial output compiles and raises at execution."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")
    for src, kw in [("FastD = Close;\n", "fastd"), ("SlowD = High + Low;\n", "slowd")]:
        code = transpile(src, trace=True, partial=True)
        compile(code, "<f>", "exec")  # must not be a SyntaxError
        assert f"pl_partial_stub('{kw}', 1)() =" not in code, \
            f"stub emitted on LHS of '=':\n{code}"
        assert f"pl_partial_stub('{kw}'" in code, f"stub not emitted for {kw}:\n{code}"
        try:
            run_el(src, bars, config, partial=True)
            raise AssertionError(f"partial run did not raise for {src!r}")
        except UnimplementedKeywordError as e:
            assert "partial-mode stub executed" in str(e), repr(str(e))
            assert kw in str(e), f"raise did not name {kw}: {str(e)!r}"


def group_i_boolean_nonshortcircuit_stub_raises():
    """FL6 BUG A: EL/PL `and`/`or` are NON-short-circuit — both operands are always
    evaluated. Python's `and`/`or` short-circuit, so a stub on the operand Python
    would skip would be BYPASSED and the boolean would silently yield a value
    instead of raising. Under partial the whole boolean must collapse to a single
    argless stub that ALWAYS raises: `A and Stub` with A false, and `A or Stub`
    with A true, both raise at execution (the exact short-circuit each op skips)."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")
    for op, left in [("and", "Close < 0"), ("or", "Close > 0")]:
        src = f"Condition1 = ({left}) {op} FakeBool(Close);\n"
        code = transpile(src, trace=True, partial=True)
        compile(code, "<f>", "exec")  # must not be a SyntaxError
        assert "pl_partial_stub('fakebool'" in code, f"stub not named fakebool:\n{code}"
        try:
            run_el(src, bars, config, partial=True)
            raise AssertionError(f"partial {op!r} did not raise (short-circuit leak)")
        except UnimplementedKeywordError as e:
            assert "partial-mode stub executed" in str(e), repr(str(e))
            assert "fakebool" in str(e), repr(str(e))
        try:
            transpile(src, trace=True)
            raise AssertionError(f"strict {op!r} did not raise")
        except UnimplementedKeywordError as e:
            assert "fakebool" in {d["name"] for d in e.errors}, repr(e.errors)


def group_j_position_keyword_index_walks_nested_unknown():
    """FL6 BUG B: the positions-ago INDEX of a single-arg position keyword must be
    WALKED so a nested unimplemented keyword is recorded (strict) and raises
    (partial). Previously the index was never visited: strict silently emitted
    kwargs.get(...) and partial reported '0 unimplemented constructs' (a watermark
    lie) then completed with a wrong value."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")
    for kw in ["MaxContracts", "EntryPrice"]:
        src = f"Value1 = {kw}(FakeF(Close));\n"
        try:
            transpile(src, trace=True)
            raise AssertionError(f"strict did not raise for {kw} (index bypass)")
        except UnimplementedKeywordError as e:
            assert {d["name"] for d in e.errors} == {"fakef"}, repr(e.errors)
        code = transpile(src, trace=True, partial=True)
        compile(code, "<f>", "exec")
        assert "pl_partial_stub('fakef'" in code, f"stub not named fakef:\n{code}"
        assert "'fakef'" in code, "watermark did not name fakef"
        try:
            run_el(src, bars, config, partial=True)
            raise AssertionError(f"partial run did not raise for {kw}")
        except UnimplementedKeywordError as e:
            assert "partial-mode stub executed" in str(e), repr(str(e))
            assert "fakef" in str(e), repr(str(e))


def group_k_switch_subject_stub_not_dropped():
    """Audit a1-F1/F2: gen_switch emitted the switch SUBJECT only inside per-case
    `if/elif <subj> == <val>:` lines. A Switch with ZERO case labels (empty body, or
    Default-only) therefore DROPPED the subject entirely — so a partial-mode stub in
    the subject was silently discarded (the run completed with a wrong value while the
    watermark still named the stub) and a Default-only switch emitted an orphan `else:`
    (a SyntaxError). EL evaluates the subject once regardless of matches, so partial
    output must COMPILE and RAISE at execution for both degenerate shapes."""
    bars = load_bars_csv(BARS_CSV)
    config = get_config("NQ")
    variants = [
        "Value1 = 5;\nSwitch (FakeF(Close)) Begin End;\nPlot1(Value1);\n",           # empty body
        "Switch (FakeF(Close)) Begin\nDefault: Value1 = 1;\nEnd;\nPlot1(Value1);\n",  # default-only
    ]
    for src in variants:
        code = transpile(src, trace=True, partial=True)
        compile(code, "<f>", "exec")  # must not be a SyntaxError (a1-F2)
        assert "pl_partial_stub('fakef'" in code, f"subject stub dropped:\n{code}"
        try:
            run_el(src, bars, config, partial=True)
            raise AssertionError(f"partial switch did not raise (stub dropped):\n{src}")
        except UnimplementedKeywordError as e:
            assert "fakef" in str(e), repr(str(e))
        try:
            transpile(src, trace=True)
            raise AssertionError(f"strict switch did not raise:\n{src}")
        except UnimplementedKeywordError as e:
            assert "fakef" in {d["name"] for d in e.errors}, repr(e.errors)


GROUPS = [
    group_a_hit_stub_execution_vs_transpile,
    group_b_guarded_stub_identical_rows,
    group_c_watermark_contract,
    group_d_default_transpile_still_raises,
    group_e_var_init_stub_raises_not_wrong_value,
    group_f_argless_stub_raises_unimplemented_not_typeerror,
    group_g_stub_survives_arg_dropping_expansions,
    group_h_stub_target_is_standalone_statement,
    group_i_boolean_nonshortcircuit_stub_raises,
    group_j_position_keyword_index_walks_nested_unknown,
    group_k_switch_subject_stub_not_dropped,
]


def main():
    passed = 0
    for g in GROUPS:
        g()
        passed += 1
        print(f"  ok: {g.__name__}")
    print(f"PARTIAL MODE: {passed}/{len(GROUPS)} groups pass")
    return 0 if passed == len(GROUPS) else 1


if __name__ == "__main__":
    sys.exit(main())
