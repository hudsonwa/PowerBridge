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

"""run_public_tier.py — the PORTABLE test tier (green out-of-the-box).

This runner executes the subset of the suite that needs NO MultiCharts captures
and NO network, so a fresh clone with only the shipped source can prove the
transpiler works before attaching any captures (see docs/CAPTURES.md). Every
member listed here ships in this repository.

It is intentionally a thin, stdlib-only subprocess/in-process runner: every
member here is an EXISTING gate/tool that already owns its own assertions - this
file adds ZERO comparison logic, it only sequences the portable members and prints
a PASS/SKIP/FAIL table.

CLASSIFICATION (per member):
  * CAPTURE-DEPENDENT + captures absent -> SKIP, with a message naming what is
    missing and how to attach it (docs/CAPTURES.md). The MultiCharts captures
    are licensed data and are not redistributed, so these skip in a fresh clone.
  * otherwise -> RUN. PASS iff the member exits 0 (or, for the in-process
    mutation pre-flight, iff its pattern check holds); FAIL otherwise. A listed
    member whose file is missing is a FAIL: the list must match the repository.

The tier is GREEN (exit 0) iff NOTHING is FAIL. See docs/verification.md for
the full list of checks you can run from this repository.

Usage:
    python3 tests/run_public_tier.py
Exit code 0 iff no member FAILed.
"""
# CONVENTION: printed output must stay ASCII-safe (no raw emoji / non-ASCII in test
# output) so it never crashes on a Windows legacy-codepage console (cp1252/cp437).
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests import capture_gate

PYTHON = sys.executable


# Each member: (label, path_relative_to_repo, extra_args, capture_dependent).
# `path` None marks the in-process mutation pre-flight (a pure pattern check that
# imports the extracted tests.mutation_test.preflight — no subprocess, no captures).
MEMBERS = [
    ("test_ast_retention",           "tests/test_ast_retention.py",                 [], False),
    ("test_catalog",                 "tests/test_catalog.py",                       [], False),
    ("test_lexer_failloud",          "tests/test_lexer_failloud.py",                [], False),
    ("test_collect_errors",          "tests/test_collect_errors.py",                [], False),
    ("test_partial_mode",            "tests/test_partial_mode.py",                  [], False),
    ("test_emit_roundtrip_regressions", "tests/test_emit_roundtrip_regressions.py",   [], False),
    ("test_runtime_config",          "tests/test_runtime_config.py",                [], False),
    ("test_runtime_encoding",        "tests/test_runtime_encoding.py",              [], False),
    ("test_f32",                     "tests/test_f32.py",                           [], False),
    ("test_persist_mode",            "tests/test_persist_mode.py",                  [], False),
    ("test_kwargs_reuse",            "tests/test_kwargs_reuse.py",                  [], False),
    ("test_kwargs_dict_call",        "tests/test_kwargs_dict_call.py",              [], False),
    ("test_trace_emit",              "tests/test_trace_emit.py",                    [], False),
    ("test_oracle",                  "tests/test_oracle.py",                        [], False),
    ("test_fill_log",                "tests/test_fill_log.py",                      [], False),
    ("test_gt_sweep",                "tests/test_gt_sweep.py",                      [], False),
    ("test_seed_warmup",             "tests/test_seed_warmup.py",                   [], False),
    ("test_bar_tape",                "tests/test_bar_tape.py",                      [], False),
    ("test_fold_literal_f32",        "tests/test_fold_literal_f32.py",              [], False),
    ("test_series_layout",           "tests/test_series_layout.py",                 [], False),
    ("test_entry_qty_zero",          "tests/test_entry_qty_zero.py",                [], False),
    ("test_no_work_fastpath",        "tests/test_no_work_fastpath.py",              [], False),
    ("test_general_entrypoint",      "tests/test_general_entrypoint.py",            [], True),
    ("mutation pre-flight",          None,                                          [], False),
    ("ground-check trap tests",      "tests/test_ground_check_traps.py",            [], False),
    ("coverage_report",              "tests/coverage_report.py",
                                     ["--parity-max-bars", "400", "--roundtrip-max-bars", "400"], True),
]


def _run_script(path, extra_args):
    """Run a member script in a fresh subprocess. Returns (ok, tail)."""
    # Members run as plain scripts; drop pytest's marker variable so one launched
    # from tests/test_public_tier.py behaves exactly as under this runner.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    proc = subprocess.run(
        [PYTHON, os.path.join(REPO, path)] + extra_args,
        cwd=REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-4:])
    return proc.returncode == 0, tail


def _run_preflight():
    """In-process mutation pre-flight (the extracted capture-free pattern check)."""
    from tests.mutation_test import preflight
    ok, problems = preflight()
    return ok, ("; ".join(problems) if problems else "all mutation patterns unique")


def main():
    have_caps = capture_gate.captures_present()

    print("=" * 74)
    print("PUBLIC TIER - portable tests (no captures, no network required)")
    print(f"  captures attached: {'yes' if have_caps else 'no (captures-less clone)'}")
    print("=" * 74)

    rows = []  # (label, status, detail)
    for label, path, extra_args, cap_dep in MEMBERS:
        if path is not None and not os.path.exists(os.path.join(REPO, path)):
            rows.append((label, "FAIL", f"{path} is listed but missing from the repository"))
            continue
        if cap_dep and not have_caps:
            rows.append((label, "SKIP",
                         "needs MultiCharts captures (not redistributed); "
                         f"attach your own: {capture_gate.CAPTURES_DOC}"))
            continue

        if path is None:
            ok, detail = _run_preflight()
        else:
            ok, detail = _run_script(path, extra_args)
        rows.append((label, "PASS" if ok else "FAIL", detail))

    print()
    n_pass = n_skip = n_fail = 0
    for label, status, detail in rows:
        if status == "PASS":
            n_pass += 1
        elif status == "SKIP":
            n_skip += 1
        else:
            n_fail += 1
        print(f"  [{status:4s}] {label:32s} {detail if status != 'PASS' else ''}".rstrip())
        if status == "FAIL" and detail:
            for ln in detail.splitlines():
                print(f"           | {ln}")

    print("\n" + "=" * 74)
    print(f"PUBLIC TIER: {n_pass} pass, {n_skip} skip, {n_fail} fail  "
          f"-> {'GREEN' if n_fail == 0 else 'FAIL'}")
    print("=" * 74)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
