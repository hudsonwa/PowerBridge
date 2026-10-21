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

"""test_reference_docs.py — the keyword reference in docs/reference/.

- Every example in docs/reference/_notes.json transpiles and runs through run_el on
  sample bars.
- The pages on disk equal tools/gen_reference.py output (its --check mode).
- _notes.json describes exactly the generator's selection, and an entry whose run
  stops says so in its description."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
TOOLS = os.path.join(REPO, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import gen_reference  # noqa: E402
from pl_transpiler import get_config, load_bars_csv, transpile  # noqa: E402

_NOTES = gen_reference.load_notes()["entries"]
_EXAMPLES = sorted((k, v["example"]) for k, v in _NOTES.items() if v.get("example"))


@pytest.mark.parametrize("name,source", _EXAMPLES, ids=[k for k, _ in _EXAMPLES])
def test_example_transpiles_and_runs(name, source):
    assert transpile(source).strip()
    bars = load_bars_csv(gen_reference.BARS_PATH)[:gen_reference.N_BARS]
    status = gen_reference.run_status(name, source, bars, get_config("NQ"))
    assert status in (gen_reference.STATUS_RUNS, gen_reference.STATUS_NOOP,
                      gen_reference.STATUS_STOPS)


def test_examples_cover_every_topic():
    topics = {_NOTES[k]["topic"] for k, _ in _EXAMPLES}
    assert topics == {t["slug"] for t in gen_reference.load_notes()["topics"]}


def test_notes_match_selection():
    names = {r["keyword"] for r in gen_reference.selected()}
    assert names == set(_NOTES)
    assert len(names) >= 190


def test_pages_up_to_date(capsys):
    rc = gen_reference.main(["--check"])
    out = capsys.readouterr().out
    assert rc == 0, out + f"\nRegenerate from the repo root with: {gen_reference.REGEN_CMD}"


def test_stops_status_is_described():
    # An entry whose run stops must say so in its own description.
    pages = gen_reference.build()
    for fname, text in pages.items():
        for block in text.split("\n### ")[1:]:
            if gen_reference.STATUS_STOPS in block:
                assert "stops with an error" in block, block.splitlines()[0]
