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

"""capture_gate.py — shared "are the MultiCharts captures attached?" probe.

Several gates (verify_roundtrip, verify_twoway, verify_full_range, the behavioural
leg of verify_cycles, test_failloud, test_general_entrypoint, and the run_gt /
golden_diff entrypoints) can only run against the per-bar MultiCharts capture CSVs
pinned in ground_truth/captures/MANIFEST.md.  Those captures are the author's
private ground truth and are NOT shipped in a public clone.

This module gives every gate one shared, side-effect-free way to ask "are the
pinned captures present?".  When they are ABSENT the gate SKIPs with a one-line
message pointing at docs/CAPTURES.md and returns success, so the suite (and the
public test tier) stays green out-of-the-box in a captures-less clone.  When the
captures ARE present the gate runs exactly as before — the skip triggers ONLY on
file absence and NEVER changes any threshold, tolerance, or coverage.

Pure stdlib + gt_manifest (which is pure data), so it imports in any clone.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

CAP = os.path.join(REPO, "ground_truth", "captures")

# Where a fresh user learns how to attach their own captures / bars.
CAPTURES_DOC = "docs/CAPTURES.md"


def missing_captures():
    """Return the sorted list of pinned capture files (from the canonical
    gt_manifest map) that are NOT present under ground_truth/captures/.  Empty
    list => every pinned capture is attached and the capture-gated gates run."""
    from tests import gt_manifest  # pure data; safe to import lazily
    missing = []
    seen = set()
    for _src, cap in gt_manifest.GT_FILES.values():
        if not cap or cap in seen:
            continue
        seen.add(cap)
        if not os.path.exists(os.path.join(CAP, cap)):
            missing.append(cap)
    return sorted(missing)


def captures_present():
    """True iff every capture pinned in gt_manifest is present on disk."""
    return not missing_captures()


def skip_message(gate):
    """One-line SKIP notice for `gate`, pointing at the BYO-captures doc."""
    return (f"[SKIP] {gate}: ground_truth/captures/ pinned files absent "
            f"(captures-less clone) — see {CAPTURES_DOC} to attach them and run "
            f"this capture-gated gate.")
