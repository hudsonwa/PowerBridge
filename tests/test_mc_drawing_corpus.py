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

"""Integrity check for your own copy of the MultiCharts drawing corpus.

The bars and readbacks are licensed and are not in the repository (see
ground_truth/drawing/README.md). When they are present, ``tapes/SHA256SUMS``
lists ``sha256  path`` for every file under ``tapes/`` and ``g-*/mc/``, with
paths relative to ground_truth/drawing/, and this module checks each file
against it. The manifest travels with the data, not with the repository.
The checker itself is tested on a small synthetic tree in any clone.
"""
import hashlib
import os

import pytest

from tests._drawing_diff import CORPUS, CORPUS_SKIP, TAPES

MANIFEST = "tapes/SHA256SUMS"


class SnapshotError(Exception):
    """A corpus file does not match its manifest."""


def _data_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        parts = rel_dir.split("/")
        if parts[0] != "tapes" and not (len(parts) >= 2 and parts[1] == "mc"):
            continue
        for name in filenames:
            rel = f"{rel_dir}/{name}"
            if rel != MANIFEST:
                found.append(rel)
    return found


def verify_snapshot(root):
    """Raise SnapshotError unless the data files match tapes/SHA256SUMS."""
    sums_path = os.path.join(root, MANIFEST)
    if not os.path.isfile(sums_path):
        raise SnapshotError(f"{MANIFEST} is missing")
    listed = {}
    with open(sums_path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                raise SnapshotError(
                    f"{MANIFEST} line {lineno} is not 'digest path'")
            listed[parts[1]] = parts[0]
    found = _data_files(root)
    missing = sorted(set(listed) - set(found))
    extra = sorted(set(found) - set(listed))
    if missing or extra:
        raise SnapshotError(f"missing={missing} extra={extra}")
    for rel, expect in sorted(listed.items()):
        with open(os.path.join(root, rel), "rb") as fh:
            got = hashlib.sha256(fh.read()).hexdigest()
        if got != expect:
            raise SnapshotError(f"{rel} sha256 {got} != {expect}")


def _write_tree(root, files):
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    lines = [f"{hashlib.sha256(data).hexdigest()}  {rel}"
             for rel, data in sorted(files.items())]
    (root / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")


_SYNTHETIC = {
    "tapes/data1.csv.gz": b"not really gzip",
    "g-x-001-a/mc/drawings.json": b"{}\n",
    "g-x-001-a/mc/trades.digest": b"0" * 64 + b"\n",
}


def test_checker_accepts_a_matching_tree(tmp_path):
    _write_tree(tmp_path, _SYNTHETIC)
    (tmp_path / "g-x-001-a" / "source.el").write_text("Vars: x(0);\n")
    verify_snapshot(str(tmp_path))


def test_checker_rejects_a_changed_file(tmp_path):
    _write_tree(tmp_path, _SYNTHETIC)
    (tmp_path / "g-x-001-a" / "mc" / "drawings.json").write_bytes(b"{ }\n")
    with pytest.raises(SnapshotError, match="drawings.json"):
        verify_snapshot(str(tmp_path))


def test_checker_rejects_missing_and_extra_files(tmp_path):
    _write_tree(tmp_path, _SYNTHETIC)
    (tmp_path / "tapes" / "data1.csv.gz").unlink()
    (tmp_path / "tapes" / "stray.txt").write_bytes(b"x")
    with pytest.raises(SnapshotError, match="missing=.*data1.*extra=.*stray"):
        verify_snapshot(str(tmp_path))


def test_checker_needs_a_manifest(tmp_path):
    with pytest.raises(SnapshotError, match="missing"):
        verify_snapshot(str(tmp_path))


@pytest.mark.skipif(not os.path.isfile(os.path.join(CORPUS, MANIFEST)),
                    reason=CORPUS_SKIP)
def test_local_corpus_matches_its_manifest():
    assert os.path.isdir(TAPES)
    verify_snapshot(CORPUS)
