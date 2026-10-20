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
mutation_test.py — proves the full-suite gate actually CATCHES regressions (#8).

A gate that is always green is worthless if it would stay green when the runtime
is broken. This harness deliberately perturbs ONE transpiler/runtime function at
a time (a real, semantically-meaningful mutation), runs a fast-but-representative
slice of the gate, and ASSERTS the gate goes RED. Any mutation the gate fails to
redden is a COVERAGE GAP and is reported loudly (not hidden).

How it works
------------
  * Mutations are exact text patches against transpiler/runtime/pl_runtime.py.
  * For each mutation: the original source is saved, the patch is applied on disk,
    a FRESH subprocess runs the gate (fresh import => the mutation is really live),
    and the gate's exit code is inspected. The gate is "RED" (mutation caught) iff
    that subprocess exits non-zero. The original source is then restored EXACTLY.
  * A try/finally guarantees the pristine source is written back even on error or
    interrupt — the harness NEVER leaves a mutation on disk.
  * After every mutation is applied+reverted, ONE final full `verify_all` run must
    be ALL GREEN, proving the revert was byte-exact and the gate works on the real
    runtime.

Fast gate (per-mutation RED detector):
    python3 tests/verify_all.py --fast --gt-only --max-bars 400
A genuine runtime mutation reddens this quickly (GT1 alone exercises TrueRange,
Average, Summation, Highest, Lowest, RSI, and the crosses path). `--fast` is
REQUIRED here: since e0927d4, plain verify_all also runs the full-range
order-column gate (~75 min) — that thorough gate is the orchestrator's DONE
gate, not this harness's red-detector. The final clean-revert proof below uses
`--fast` for the same reason (full window, all labels, no full-range
order sweep); the byte-exact source comparison independently proves the revert.

Integrity floor: this harness proves the gate works; it changes NO production
behaviour. It never weakens a tolerance, a deferral, or a test. The source file is
identical before and after a successful run.

Usage:
    python3 tests/mutation_test.py
Exit code 0 iff EVERY mutation was caught AND the final full verify_all is GREEN.
"""
import os
import sys
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(REPO, "pl_transpiler", "runtime", "pl_runtime.py")
VERIFY = os.path.join(REPO, "tests", "verify_all.py")

# Each mutation: (id, human description, target function, old_string, new_string).
# old_string MUST appear exactly once in the runtime source (anchored with enough
# context where the bare line is ambiguous, e.g. pl_true_range vs the ADX helper).
MUTATIONS = [
    (
        "true_range_sign_flip",
        "flip a sign in pl_true_range (high - low -> high + low)",
        "pl_true_range",
        'def pl_true_range(high, low, prev_close):\n'
        '    """True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))"""\n'
        '    return max(high - low, abs(high - prev_close), abs(low - prev_close))',
        'def pl_true_range(high, low, prev_close):\n'
        '    """True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))"""\n'
        '    return max(high + low, abs(high - prev_close), abs(low - prev_close))',
    ),
    (
        "average_off_by_one",
        "off-by-one window in pl_average (divide by length + 1)",
        "pl_average",
        "return f32(sum(window) / length)",
        "return f32(sum(window) / (length + 1))",
    ),
    (
        "summation_off_by_one",
        "off-by-one window in pl_summation (drop first element: window[1:])",
        "pl_summation",
        "return f32(sum(window))",
        "return f32(sum(window[1:]))",
    ),
    (
        "highest_swap_min",
        "swap max->min in pl_highest",
        "pl_highest",
        "return f32(max(window))",
        "return f32(min(window))",
    ),
    (
        "lowest_swap_max",
        "swap min->max in pl_lowest",
        "pl_lowest",
        "return f32(min(window))",
        "return f32(max(window))",
    ),
    (
        "rsi_wilder_factor",
        "perturb the Wilder smoothing factor in pl_rsi ((length - 1) -> (length - 2))",
        "pl_rsi",
        # NOTE: pattern refreshed 2026-07-02 — U7b rewrote pl_rsi to the incremental
        # double-precision Wilder recursion, which relocated the smoothing factor.
        "st['avg_g'] = (st['avg_g'] * (length - 1) + g) / length",
        "st['avg_g'] = (st['avg_g'] * (length - 2) + g) / length",
    ),
    (
        "crosses_above_comparison",
        "alter the comparison in pl_crosses_above (> -> <)",
        "pl_crosses_above",
        "return series_a[-1] > series_b[-1] and _cross_prior_relation(series_a, series_b, start) < 0",
        "return series_a[-1] < series_b[-1] and _cross_prior_relation(series_a, series_b, start) < 0",
    ),
]


def run_gate(args):
    """Run verify_all in a fresh subprocess. Returns (returncode, tail_of_output)."""
    proc = subprocess.run(
        [sys.executable, VERIFY] + args,
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-3:])
    return proc.returncode, tail


def preflight():
    """Capture-free pattern check: every mutation's old_string must be uniquely
    locatable in the live runtime source, so a mutation patches exactly what we
    think it does. Needs NO captures / subprocess gate, so the public tier runs it
    directly. Returns (ok, list_of_problem_strings); ok is False iff any old_string
    occurs a number of times other than exactly once (a stale/ambiguous pattern)."""
    with open(RUNTIME, "r", encoding="utf-8") as f:
        source = f.read()
    problems = []
    for mid, desc, fn, old, new in MUTATIONS:
        cnt = source.count(old)
        if cnt != 1:
            problems.append(f"mutation '{mid}' ({fn}) old_string occurs {cnt} times "
                            f"(expected exactly 1)")
    return (not problems), problems


def main():
    with open(RUNTIME, "r", encoding="utf-8") as f:
        original = f.read()

    # Pre-flight: every old_string must be uniquely locatable, so a mutation
    # patches exactly what we think it does.
    ok, problems = preflight()
    if not ok:
        for p in problems:
            print(f"ABORT: {p} - refusing to run to avoid an "
                  f"ambiguous/incorrect patch.")
        return 2

    results = []  # (mid, desc, fn, caught, detail)
    try:
        # Sanity: the clean gate must be GREEN before we trust RED to mean "caught".
        print("=" * 74)
        print("MUTATION TESTING - proving verify_all catches perturbed runtime fns")
        print("=" * 74)
        print("\n[baseline] clean fast gate (verify_all --fast --gt-only --max-bars 400)...")
        rc0, tail0 = run_gate(["--fast", "--gt-only", "--max-bars", "400"])
        if rc0 != 0:
            print(f"  ABORT: baseline gate is NOT green (rc={rc0}). Cannot attribute "
                  f"RED to a mutation.\n  {tail0}")
            return 2
        print("  baseline GREEN (rc=0)")

        for mid, desc, fn, old, new in MUTATIONS:
            mutated = original.replace(old, new, 1)
            assert mutated != original, f"mutation {mid} produced no change"
            with open(RUNTIME, "w", encoding="utf-8") as f:
                f.write(mutated)
            try:
                rc, tail = run_gate(["--fast", "--gt-only", "--max-bars", "400"])
            finally:
                # Restore IMMEDIATELY so a crash mid-loop can't leave it dirty.
                with open(RUNTIME, "w", encoding="utf-8") as f:
                    f.write(original)
            caught = rc != 0
            results.append((mid, desc, fn, caught, f"rc={rc}"))
            mark = "CAUGHT (RED)" if caught else "MISSED (still GREEN)"
            print(f"\n[{mid}] {fn}: {desc}")
            print(f"    -> gate {mark}  (rc={rc})")
            if not caught:
                print(f"    !! COVERAGE GAP: the gate did NOT redden for this mutation.")
                print(f"    !! gate tail: {tail}")
    finally:
        # Belt-and-suspenders: guarantee pristine source on every exit path.
        with open(RUNTIME, "w", encoding="utf-8") as f:
            f.write(original)

    # Confirm the on-disk source is byte-exact to the pristine original.
    with open(RUNTIME, "r", encoding="utf-8") as f:
        restored = f.read()
    clean_revert = (restored == original)

    missed = [r for r in results if not r[3]]

    print("\n" + "=" * 74)
    print("MUTATION SCOREBOARD")
    print("=" * 74)
    for mid, desc, fn, caught, detail in results:
        mark = "CAUGHT" if caught else "MISSED"
        print(f"  [{mark}] {mid:26s} {fn:18s} {detail}")
    print(f"\n  caught {len(results) - len(missed)}/{len(results)} mutations; "
          f"source revert byte-exact: {clean_revert}")
    if missed:
        print("\n  COVERAGE GAPS (mutations the gate FAILED to catch):")
        for mid, desc, fn, _, _ in missed:
            print(f"    - {mid} ({fn}): {desc}")

    # Final proof: a clean FULL verify_all must be ALL GREEN (proves clean revert
    # AND that the gate passes on the genuine runtime).
    print("\n" + "=" * 74)
    print("FINAL FULL GATE - python3 tests/verify_all.py --fast  (must be ALL GREEN)")
    print("=" * 74)
    rcf, tailf = run_gate(["--fast"])
    final_green = rcf == 0
    print(tailf)
    print(f"\n  final full verify_all: {'ALL GREEN' if final_green else 'RED'} (rc={rcf})")

    ok = (not missed) and clean_revert and final_green
    print("\n" + "=" * 74)
    print(f"RESULT: {'PASS - gate proven to catch regressions' if ok else 'FAIL'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
