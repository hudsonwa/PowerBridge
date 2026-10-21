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
test_runtime_encoding.py — proves pl_runtime's loud stubs install without reading
any source file (Windows finding B2; .pyc-only / zipped installs).

pl_runtime wraps 340 trivial pl_* stubs in loud wrappers at import time (see
`_install_loud_stubs`). It used to find them by reading its OWN .py sources with
`ast`; a locale-codec decode error there once silently disabled every loud stub,
and the read cannot work at all from a .pyc-only or zipped install. The names now
come from the generated, static `_loud_stub_names` module.

This test, entirely capture-free and portable, checks:

  (a) ALL 340 loud stubs install (counted at runtime AND cross-checked against an
      independent AST recount of the sources, so a silently-empty install fails).

  (b) The static list is in sync with the runtime sources (regenerate with
      `python tools/gen_loud_stubs.py`).

  (c) Installing never opens a runtime source file: with builtins.open forced to
      fail on them, the full set still installs and nothing is written to stderr.

Exit 0 iff all three hold.
"""
import builtins
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pl_transpiler.runtime.pl_runtime as rt
import pl_transpiler.runtime._rt_core as rt_core
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "gen_loud_stubs", os.path.join(REPO, "tools", "gen_loud_stubs.py"))
gen_loud_stubs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_loud_stubs)

# The runtime source files the old self-read used to open.
RUNTIME_FILES = [sys.modules["pl_transpiler.runtime." + n].__file__
                 for n in rt_core._RUNTIME_SUBMODULES]

# The documented number of loud stubs pl_runtime installs.
EXPECTED_LOUD_STUBS = 340


def _installed_loud_stub_names():
    """Names currently bound on the runtime module to loud stubs (the live count)."""
    return {n for n in dir(rt)
            if getattr(getattr(rt, n), "_pl_loud_stub", False)}


def test_all_loud_stubs_install():
    """(a) All 340 loud stubs install and match an independent AST recount."""
    installed = _installed_loud_stub_names()
    recount = gen_loud_stubs.scan() - rt._UNIMPL_WHITELIST
    assert len(installed) == EXPECTED_LOUD_STUBS, (
        f"installed {len(installed)} loud stubs, expected {EXPECTED_LOUD_STUBS}")
    assert installed == recount, (
        "installed loud-stub set does not match the independent AST recount "
        f"(installed-only: {sorted(installed - recount)[:5]}, "
        f"recount-only: {sorted(recount - installed)[:5]})")
    print(f"  [ok] all {len(installed)} loud stubs install "
          "(cross-checked against independent AST recount)")


def test_static_list_in_sync():
    """(b) The generated _loud_stub_names module matches the runtime sources."""
    path = os.path.join(REPO, "pl_transpiler", "runtime", "_loud_stub_names.py")
    with open(path, "r", encoding="utf-8") as f:
        current = f.read()
    assert current == gen_loud_stubs.render(gen_loud_stubs.scan()), (
        "pl_transpiler/runtime/_loud_stub_names.py is out of date; "
        "run: python tools/gen_loud_stubs.py")
    print("  [ok] static loud-stub list is in sync with the runtime sources")


def test_install_reads_no_source():
    """(c) _install_loud_stubs never opens a runtime .py file."""
    real_open = builtins.open

    def failing_open(file, *args, **kwargs):
        if file in RUNTIME_FILES:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1,
                                     "forced decode failure (simulated locale)")
        return real_open(file, *args, **kwargs)

    captured = io.StringIO()
    real_stderr = sys.stderr
    try:
        builtins.open = failing_open
        sys.stderr = captured
        rt._install_loud_stubs()
    finally:
        builtins.open = real_open
        sys.stderr = real_stderr
    assert len(_installed_loud_stub_names()) == EXPECTED_LOUD_STUBS
    assert captured.getvalue() == "", captured.getvalue()
    print("  [ok] loud stubs install without reading any runtime source")


def main():
    tests = [
        test_all_loud_stubs_install,
        test_static_list_in_sync,
        test_install_reads_no_source,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL RUNTIME-ENCODING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
