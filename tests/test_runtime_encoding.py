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
test_runtime_encoding.py — proves pl_runtime's self-introspection is codec-safe
and fail-loud (Windows finding B2).

pl_runtime installs 371 loud stubs by reading its OWN source with `ast` at import
time (see `_install_loud_stubs`). The read used to use the locale default codec;
on a legacy codepage (cp932/936/949) or an ascii/LC_ALL=C locale a non-ASCII byte
in the file raised UnicodeDecodeError, which the bare `except Exception: return`
then SWALLOWED — so every loud stub was silently NOT installed and unimplemented
EasyLanguage builtins would silently return plausible-but-wrong 0/'' values. That
is the worst failure mode this project has.

This test falsifies the fix two ways, entirely capture-free and portable:

  (a) With the utf-8 self-read, the introspection succeeds and ALL 371 loud stubs
      install (counted at runtime AND cross-checked against an independent AST
      recount, so a silently-empty install fails here).

  (b) When a decode failure IS forced (builtins.open raising UnicodeDecodeError on
      the self-read) UNDER PL_STRICT, `_install_loud_stubs` RAISES loudly instead
      of silently returning — fail-loud, never silent-wrong.

  (c) The same forced failure WITHOUT PL_STRICT stays tolerant (does not raise) but
      emits exactly one loud stderr warning — the degraded state is never invisible.

Exit 0 iff all three hold.
"""
import ast as _ast
import builtins
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pl_transpiler.runtime.pl_runtime as rt

# The documented number of loud stubs pl_runtime installs from its own source.
EXPECTED_LOUD_STUBS = 371


def _installed_loud_stub_names():
    """Names currently bound on the runtime module to loud stubs (the live count)."""
    return {n for n in dir(rt)
            if getattr(getattr(rt, n), "_pl_loud_stub", False)}


def _ast_recount_stub_names():
    """Independently recompute the stub name set the way _install_loud_stubs does,
    so (a) cannot pass on a silently-empty install. Mirrors the module's own
    _is_stub / name-filter logic against the utf-8 source."""
    with open(rt.__file__, "r", encoding="utf-8") as f:
        tree = _ast.parse(f.read())

    def is_stub(fn):
        body = list(fn.body)
        if (body and isinstance(body[0], _ast.Expr)
                and isinstance(getattr(body[0], "value", None), _ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        if len(body) == 1 and isinstance(body[0], _ast.Return):
            v = body[0].value
            return v is None or (isinstance(v, _ast.Constant)
                                 and v.value in (0, 0.0, None, ''))
        if len(body) == 1 and isinstance(body[0], _ast.Pass):
            return True
        return False

    names = {n.name for n in _ast.walk(tree)
             if isinstance(n, _ast.FunctionDef)
             and n.name.startswith("pl_") and is_stub(n)}
    return names - rt._UNIMPL_WHITELIST


def _run_install_with_failing_selfread(strict):
    """Call _install_loud_stubs with builtins.open forced to raise
    UnicodeDecodeError on the runtime self-read (simulating a legacy/ascii locale).
    Sets/restores PL_STRICT and captures stderr. Returns (raised, stderr_text).
    Always restores real open + _PL_STRICT + a clean stub install afterwards."""
    real_open = builtins.open
    orig_strict = rt._PL_STRICT

    def failing_open(file, *args, **kwargs):
        if file == rt.__file__:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1,
                                     "forced decode failure (simulated locale)")
        return real_open(file, *args, **kwargs)

    captured = io.StringIO()
    real_stderr = sys.stderr
    raised = False
    try:
        rt._PL_STRICT = strict
        builtins.open = failing_open
        sys.stderr = captured
        try:
            rt._install_loud_stubs()
        except RuntimeError:
            raised = True
    finally:
        builtins.open = real_open
        sys.stderr = real_stderr
        rt._PL_STRICT = orig_strict
        # Re-install cleanly so the module is not left degraded for later tests.
        rt._install_loud_stubs()
    return raised, captured.getvalue()


def test_utf8_selfread_installs_all_loud_stubs():
    """(a) The utf-8 self-read succeeds and all 371 loud stubs install."""
    installed = _installed_loud_stub_names()
    recount = _ast_recount_stub_names()
    assert len(installed) == EXPECTED_LOUD_STUBS, (
        f"installed {len(installed)} loud stubs, expected {EXPECTED_LOUD_STUBS} — "
        "the utf-8 self-read did NOT install the full loud-stub set")
    assert installed == recount, (
        "installed loud-stub set does not match the independent AST recount "
        f"(installed-only: {sorted(installed - recount)[:5]}, "
        f"recount-only: {sorted(recount - installed)[:5]})")
    assert len(recount) == EXPECTED_LOUD_STUBS, (
        f"independent AST recount found {len(recount)} stubs, expected "
        f"{EXPECTED_LOUD_STUBS}")
    print(f"  [ok] utf-8 self-read installs all {len(installed)} loud stubs "
          "(cross-checked against independent AST recount)")


def test_strict_decode_failure_raises_loudly():
    """(b) Under PL_STRICT a forced self-read decode failure RAISES, never silently
    returns (which would leave unimplemented builtins returning wrong 0/'')."""
    raised, _ = _run_install_with_failing_selfread(strict=True)
    assert raised, (
        "under PL_STRICT a UnicodeDecodeError on the runtime self-read did NOT "
        "raise — the fail-loud guard is broken; loud stubs would be silently "
        "skipped and unimplemented builtins would return plausible-but-wrong 0/''")
    # And the clean re-install restored the full loud-stub set.
    assert len(_installed_loud_stub_names()) == EXPECTED_LOUD_STUBS, (
        "loud stubs not restored to full count after the strict-failure test")
    print("  [ok] PL_STRICT self-read decode failure raises loudly (fail-loud)")


def test_nonstrict_decode_failure_warns_but_tolerates():
    """(c) Without PL_STRICT the same failure stays tolerant (no raise) but emits
    exactly one loud stderr warning — the degraded state is never invisible."""
    raised, stderr_text = _run_install_with_failing_selfread(strict=False)
    assert not raised, (
        "without PL_STRICT the self-read decode failure raised — the tolerant "
        "fallback regressed")
    assert "WARNING" in stderr_text and "loud stubs are NOT installed" in stderr_text, (
        f"expected one loud stderr warning about skipped loud stubs, got: "
        f"{stderr_text!r}")
    assert stderr_text.count("WARNING") == 1, (
        f"expected exactly one warning line, got {stderr_text.count('WARNING')}: "
        f"{stderr_text!r}")
    print("  [ok] non-strict self-read decode failure warns once but tolerates")


def main():
    tests = [
        test_utf8_selfread_installs_all_loud_stubs,
        test_strict_decode_failure_raises_loudly,
        test_nonstrict_decode_failure_warns_but_tolerates,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL RUNTIME-ENCODING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
