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

"""repoint.py — shared scratch-dir + GT_FILES repoint plumbing for the reverse gates.

The round-trip / two-way / cycle gates all do the SAME two mechanical things:

  1. mint a unique per-run predicted-CSV working dir under .scratch/ (auto-removed),
     so concurrent gate runs never collide on the same file; and
  2. write an emitted EL text per GT label under ground_truth/<dir>/ and repoint BOTH
     run_gt.GT_FILES and golden_diff.GT_FILES at it (capture name preserved), then
     restore both dicts afterwards.

Previously each gate hand-rolled these (4 near-copies of the repoint loop, 6 copies of
the scratch helper). They are unified here. Two invariants are preserved EXACTLY:

  * run_gt.GT_FILES and golden_diff.GT_FILES are two DISTINCT dict objects that
    verify_all shares BY REFERENCE — so the repoint MUTATES them in place (clear+update
    on restore), never rebinds; a rebind would not propagate to verify_all.
  * repoint_files SAVE/RESTOREs both dicts (contextmanager). verify_cycles already did
    this; round-trip/twoway used to mutate permanently. Save/restore is strictly safer
    (nothing downstream in-process relies on the mutation surviving the block) and is
    now the single policy everywhere.

All run_gt / golden_diff imports are LAZY (inside the functions) so this module stays
importable with only `tests/` on sys.path and cannot create an import cycle.
"""
import atexit
import contextlib
import os
import shutil
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def new_scratch(prefix):
    """Unique per-run working dir under REPO/.scratch/, auto-removed at exit.

    prefix identifies the caller in the dir name (e.g. "rt-", "va-"); it has no
    observable effect on any gate result — it only disambiguates temp dirs."""
    base = os.path.join(REPO, ".scratch")
    os.makedirs(base, exist_ok=True)
    d = tempfile.mkdtemp(prefix=prefix, dir=base)
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d


def emit_texts(emit_fn, labels):
    """Read each label's ORIGINAL EL source (via run_gt.GT_FILES) and build the
    emitted text with emit_fn(source_text). Returns {label: emitted_text}.

    Exceptions from emit_fn propagate — callers that need continue-on-error
    semantics (twoway) build their own texts dict instead of using this helper.
    GTA6_1/GTA6_2 share one source file; each still gets its own emitted text."""
    from tests import run_gt
    texts = {}
    for label in labels:
        orig_src_name = run_gt.GT_FILES[label][0]
        with open(os.path.join(run_gt.GT, orig_src_name), encoding="utf-8") as f:
            source_text = f.read()
        texts[label] = emit_fn(source_text)
    return texts


@contextlib.contextmanager
def repoint_files(dirname, texts_by_label, name=None):
    """Write each label's emitted text under ground_truth/<dirname>/ and repoint BOTH
    run_gt.GT_FILES and golden_diff.GT_FILES at it (capture name preserved verbatim),
    for the duration of the `with` block; restore both dicts on exit.

    dirname          : subdir of ground_truth/ (e.g. ".roundtrip"); created if absent.
    texts_by_label   : {label: emitted_el_text}. Only these labels are repointed.
    name             : label -> filename (default "<label>.txt"). Lets the mutation-
                       reflect path tag files "<label>_<tag>.txt".

    Yields the list of repointed labels. Patches EXISTING label keys only — a new
    label name would silently lose the per-label data feed keyed by that name."""
    from tests import run_gt
    from tests import golden_diff
    if name is None:
        name = lambda label: f"{label}.txt"

    os.makedirs(os.path.join(run_gt.GT, dirname), exist_ok=True)
    saved_run = dict(run_gt.GT_FILES)
    saved_gold = dict(golden_diff.GT_FILES)
    try:
        for label, text in texts_by_label.items():
            cap_name = run_gt.GT_FILES[label][1]
            out_name = os.path.join(dirname, name(label))
            with open(os.path.join(run_gt.GT, out_name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            run_gt.GT_FILES[label] = (out_name, cap_name)
            golden_diff.GT_FILES[label] = (out_name, cap_name)
        yield list(texts_by_label.keys())
    finally:
        run_gt.GT_FILES.clear()
        run_gt.GT_FILES.update(saved_run)
        golden_diff.GT_FILES.clear()
        golden_diff.GT_FILES.update(saved_gold)
