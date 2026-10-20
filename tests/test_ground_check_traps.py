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

"""test_ground_check_traps.py — prove mc_ground_check's Pass 2 (return-type) and Pass 3b (arity)
catch the traps they were built for, while a known-good GT script still passes.

  Pass 2: a string-returning keyword stored in a NUMERIC var (the SymbolCurrencyCode
          archetype; probed here with ExitName, which the catalog still classifies as
          string-returning — SymbolCurrencyCode itself was reclassified numeric).
  Pass 3b: a keyword with min_arity>0 called with too few args.
  Regression guard: ground_truth/GT1_functions_indicators.txt must still exit 0.
"""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GATE = os.path.join(REPO, 'pl_transpiler', 'tools', 'mc_ground_check.py')


def run_gate(text):
    """Write `text` to a temp .txt, run mc_ground_check on it, return (exit_code, output)."""
    fd, path = tempfile.mkstemp(suffix='.txt')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        p = subprocess.run([sys.executable, GATE, path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr
    finally:
        os.remove(path)


def run_gate_path(path):
    p = subprocess.run([sys.executable, GATE, path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + p.stderr


def main():
    failures = []

    # --- Pass 2: string-returning keyword (ExitName) stored in a NUMERIC var ---
    # ExitName is a zero-arg string-returning property, the same shape as the
    # SymbolCurrencyCode archetype; SymbolCurrencyCode itself is now catalog-classified
    # numeric, so ExitName is the live probe that keeps this trap exercisable.
    rc, out = run_gate("Vars: x( 0 );\nx = ExitName;\n")
    if rc == 0:
        failures.append("Pass 2: expected exit 1 for ExitName-into-numeric, got 0")
    if 'exitname' not in out.lower():
        failures.append("Pass 2: output did not cite ExitName")
    if 'RETURN-TYPE' not in out:
        failures.append("Pass 2: output did not name the RETURN-TYPE trap")
    print(f"[Pass 2 return-type] exit={rc} (expected 1)")

    # --- Pass 3b: a keyword with min_arity>0 called with too few args ---
    # Convert_Currency has min_arity 4 and is absent from every PROVEN script.
    rc, out = run_gate("Vars: y( 0 );\ny = Convert_Currency( 1 );\n")
    if rc == 0:
        failures.append("Pass 3b: expected exit 1 for Convert_Currency too-few-args, got 0")
    if 'convert_currency' not in out.lower():
        failures.append("Pass 3b: output did not cite Convert_Currency")
    if 'ARITY' not in out:
        failures.append("Pass 3b: output did not name the ARITY trap")
    print(f"[Pass 3b arity]       exit={rc} (expected 1)")

    # --- Regression: a known-good GT script still passes (exit 0) ---
    good = os.path.join(REPO, 'ground_truth', 'GT1_functions_indicators.txt')
    rc, out = run_gate_path(good)
    if rc != 0:
        failures.append(f"known-good GT1 regressed: expected exit 0, got {rc}\n{out}")
    print(f"[known-good GT1]      exit={rc} (expected 0)")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nALL TRAP TESTS PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
