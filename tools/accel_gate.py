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

"""accel_gate.py — the native-vs-Python canonical-digest gate.

Standalone CI gate. Its first step builds/installs the native cdylib; then it
runs the reference strategy through the NATIVE path and the Python path and
compares the canonical digest. It also asserts the native path was actually
taken: the extension imported *and* the fallback counter is unchanged, so a
digest that came from a silent Python fallback is a FAIL, never a (false) pass.

Exit codes:

    0   PASS (native taken, digests identical) or SKIP (cdylib cannot be built).
    1   FAIL (digest mismatch, or the extension is present but fell back).
    2   usage error.

    --prove-mismatch   run a deliberately broken backend; exit 0 only if the gate
                       detects the mismatch (i.e. the gate can actually fail).
    --prove-fallback   simulate a fallback-fired run; exit 0 only if the gate
                       reports it as a FAIL, not a pass.

TRUST MODEL: this gate EXECUTES generated Python and a compiled kernel — run it
only on inputs you trust.
"""
import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pl_transpiler.runtime.accel as accel_mod  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import (  # noqa: E402
    count_round_turns,
    fills_sha256,
    format_digest,
    run_oracle,
)
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")

PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"


def _build_cdylib():
    """First step: build + install the native extension next to the source tree."""
    script = os.path.join(REPO, "tools", "build_accel.sh")
    proc = subprocess.run(["bash", script], cwd=REPO)
    return proc.returncode


def _load_reference():
    el_source = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLES)
    return el_source, bars, get_config("NQ")


def _compare():
    """Run native + Python, return (outcome, exit_code, message)."""
    el_source, bars, config = _load_reference()

    fallbacks_before = accel_mod.fallback_count()
    try:
        native_fills = accel_mod.run_native(el_source, bars, config)
    except accel_mod.AccelUnavailable as exc:
        return SKIP, 0, f"native extension not built: {exc}"
    except accel_mod.AccelRefused as exc:
        return FAIL, 1, (
            "extension present but the native path refused the program "
            f"(a Python fallback would be a false green): {exc}"
        )

    if accel_mod.fallback_count() != fallbacks_before:
        return FAIL, 1, (
            "the native path fell back mid-run; the digest would be a Python "
            "fallback digest, not a native digest"
        )

    python_digest = run_oracle(el_source, bars, config, max_bars_back=0)
    native_sha = fills_sha256(native_fills)
    if native_sha != python_digest["sha256"]:
        return FAIL, 1, (
            f"native/Python fill sha256 mismatch: native={native_sha} "
            f"python={python_digest['sha256']}"
        )
    native_text = format_digest(
        fills=len(native_fills),
        round_turns=count_round_turns(native_fills),
        sha256=native_sha,
    )
    if native_text != python_digest["text"]:
        return FAIL, 1, "native/Python canonical digest text mismatch"

    return PASS, 0, (
        f"digests identical ({len(native_fills)} fills, "
        f"{count_round_turns(native_fills)} round-turns, sha256={native_sha[:16]}…) "
        "with the native path taken"
    )


def _gate():
    """The real gate: build first, then compare. Returns (outcome, exit_code, msg)."""
    rc = _build_cdylib()
    if rc != 0:
        return SKIP, 0, "cannot build the native cdylib (no Rust toolchain?)"
    if not accel_mod.accel_available():
        return FAIL, 1, "native extension was built but cannot be imported"
    return _compare()


def _prove_mismatch():
    """Break the backend deliberately and prove the gate reports FAIL."""
    if not accel_mod.accel_available():
        return SKIP, 0, "native extension not built; cannot self-verify"
    el_source, bars, config = _load_reference()
    real_run_native = accel_mod.run_native

    def broken(_el, _bars, _config):
        return real_run_native(_el, _bars, _config)[:-1]

    accel_mod.run_native = broken
    try:
        outcome, code, msg = _compare()
    finally:
        accel_mod.run_native = real_run_native
    if outcome == FAIL:
        return PASS, 0, f"detected deliberate mismatch ({msg})"
    return FAIL, 1, f"deliberately broken backend was NOT detected (outcome={outcome})"


def _prove_fallback():
    """Simulate a fallback-fired run and prove the gate reports FAIL, not pass."""
    if not accel_mod.accel_available():
        return SKIP, 0, "native extension not built; cannot self-verify"
    real_run_native = accel_mod.run_native

    def fallback(_el, _bars, _config):
        raise accel_mod.AccelRefused("simulated kernel refusal")

    accel_mod.run_native = fallback
    try:
        outcome, code, msg = _compare()
    finally:
        accel_mod.run_native = real_run_native
    if outcome == FAIL:
        return PASS, 0, f"fallback-fired run reported as FAIL ({msg})"
    return FAIL, 1, (
        f"fallback-fired run was NOT reported as FAIL (outcome={outcome})"
    )


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="accel_gate",
        description="Build the native cdylib, run native + Python, and compare "
                    "the canonical trade-list digest (native path must be taken).",
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "--prove-mismatch",
        action="store_true",
        help="run a deliberately broken backend and verify the gate can fail",
    )
    group.add_argument(
        "--prove-fallback",
        action="store_true",
        help="simulate a fallback-fired run and verify it is a FAIL, not a pass",
    )
    return ap


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.prove_mismatch:
        outcome, code, msg = _prove_mismatch()
    elif args.prove_fallback:
        outcome, code, msg = _prove_fallback()
    else:
        outcome, code, msg = _gate()
    print(f"[accel_gate] {outcome}: {msg}")
    return code


if __name__ == "__main__":
    sys.exit(main())
