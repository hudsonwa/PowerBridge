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

"""test_docs_stale_phrases.py — docs must not describe behaviour that changed.

Since 0.11.0 `--max-bars-back` defaults to `auto`, and the native kernel runs
`Print` and numeric arrays. Phrases from the older docs that contradict this fail the test.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STALE = [
    (re.compile(r"default stays 0", re.I), "MaxBarsBack default is auto"),
    (re.compile(r"MaxBarsBack is opt-in", re.I), "MaxBarsBack default is auto"),
    (re.compile(r"max-bars-back[^.\n]{0,40}\bopt-in\b", re.I),
     "MaxBarsBack default is auto"),
    (re.compile(r"evaluated from bar 1 by default", re.I),
     "MaxBarsBack default is auto"),
    (re.compile(r"By default strategies are evaluated from bar 1", re.I),
     "MaxBarsBack default is auto"),
    (re.compile(r"Still refused:[^.]*`Print`", re.I | re.S),
     "the native kernel runs Print"),
    (re.compile(r"skips\s+the\s+native-kernel\s+tests", re.I),
     "native-kernel tests are not collected without the kernel"),
    (re.compile(r"fill\s+counts\s+quoted\s+in\s+the\s+README", re.I),
     "the README quotes no fill counts"),
    (re.compile(r"does\s+not\s+yet\s+run\s+strategies\s+that\s+declare\s+\**arrays", re.I),
     "the native kernel runs numeric and true/false arrays"),
]


def _doc_files():
    yield ROOT / "README.md"
    yield from sorted((ROOT / "docs").rglob("*.md"))


def test_no_stale_phrases_in_docs():
    hits = []
    for path in _doc_files():
        text = path.read_text(encoding="utf-8")
        for pattern, why in STALE:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append("%s:%d: %r (%s)" % (
                    path.relative_to(ROOT), line, m.group(0)[:60], why))
    assert not hits, "stale doc phrases:\n" + "\n".join(hits)
