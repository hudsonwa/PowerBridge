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

"""guard_ground_truth.py — loop guardrail.

Asserts the ground-truth captures are byte-identical to the sha256 values pinned
in ground_truth/captures/MANIFEST.md, and that the gate's tolerances/required
counts have not been weakened. Run by the autonomous loop driver BEFORE each
worker turn; a non-zero exit aborts the loop (a worker tried to game the gate).

The captures are the independent evaluator's ground truth. They are READ-ONLY.
"""
import hashlib
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(REPO, "ground_truth", "captures")
MANIFEST = os.path.join(CAP, "MANIFEST.md")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pinned_hashes():
    """Parse MANIFEST.md table rows: | `name.csv` ... | <sha256> |"""
    pinned = {}
    if not os.path.exists(MANIFEST):
        return pinned
    for line in open(MANIFEST, errors="ignore"):
        if not line.strip().startswith("|"):
            continue
        # ~~`name.csv`~~ rows are explicitly superseded history (e.g. the pre-v3
        # GTA5 capture) — kept in the MANIFEST as provenance, not a live pin.
        if re.search(r"~~\s*`[^`]+\.csv`\s*~~", line):
            continue
        m_name = re.search(r"`([^`]+\.csv)`", line)
        m_hash = re.search(r"\b([0-9a-f]{64})\b", line)
        if m_name and m_hash:
            pinned[m_name.group(1)] = m_hash.group(1)
    return pinned


def main():
    failures = []

    # 1. Every pinned capture must EXIST and match its hash. (Existence matters:
    #    in a fresh worktree the captures are symlinks that must be re-created;
    #    a guard that skips missing files passes vacuously on broken wiring.)
    pinned = pinned_hashes()
    for name, want in pinned.items():
        path = os.path.join(CAP, name)
        if not os.path.exists(path):
            failures.append(f"CAPTURE MISSING: {name} is pinned in MANIFEST but absent "
                            f"(broken/missing symlink into the main checkout?)")
        else:
            got = sha256(path)
            if got != want:
                failures.append(f"CAPTURE TAMPERED: {name} sha256 {got[:12]} != pinned {want[:12]}")

    # 2. No uncommitted edits to the gate's tolerance/required-count logic.
    #    (The loop may ADD GTA6 entries, but must not weaken rtol/atol or shrink
    #    an existing required count. We flag ANY working-tree change to these
    #    files for the orchestrator/verifier to inspect — fail-closed.)
    for rel in ("tests/golden_diff.py",):
        try:
            diff = subprocess.run(
                ["git", "-C", REPO, "diff", "--", rel],
                capture_output=True, text=True).stdout
        except Exception:
            diff = ""
        if re.search(r"^[+-].*(rtol|atol)\s*=", diff, re.M):
            failures.append(f"TOLERANCE EDIT in {rel} (working tree) — needs human review")

    # 3. No uncommitted edits to ANY gate file. A worker that authors a gate must
    #    not later weaken it invisibly (the independent verdict only sees
    #    diff --stat). Gate changes may only land via a reviewed checkpoint
    #    commit; any working-tree diff to tests/verify_*.py is fail-closed.
    #    Exception: GUARD_ALLOW_GATE_EDITS (comma-separated relpaths) — set ONLY
    #    inline in a units.json check line (part of the human-reviewed charter,
    #    never worker-controlled) for units whose brief legitimately edits a gate.
    allowed = {p.strip() for p in
               os.environ.get("GUARD_ALLOW_GATE_EDITS", "").split(",") if p.strip()}
    try:
        gate_diff = subprocess.run(
            ["git", "-C", REPO, "diff", "--name-only", "--", "tests/verify_*.py"],
            capture_output=True, text=True).stdout.strip()
    except Exception:
        gate_diff = ""
    if gate_diff:
        for name in gate_diff.splitlines():
            if name not in allowed:
                failures.append(f"GATE EDIT in {name} (working tree) — needs human review")

    if failures:
        print("GROUND-TRUTH GUARD FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("ground-truth guard OK (%d capture(s) verified)" % sum(
        1 for n in pinned if os.path.exists(os.path.join(CAP, n))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
