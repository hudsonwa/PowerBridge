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


"""Docs mark what is on main but not released as *new in the next release (#N)*,
and those markers stay in step with the CHANGELOG's Unreleased section.

- Every marker's #N is a pull request listed under Unreleased, so cutting a
  release (emptying Unreleased) fails here until the markers are removed.
- A command-line flag that an Unreleased entry introduces carries that entry's
  marker in every doc that mentions it.
- The README describes released features only, so it has no markers.
"""

import glob
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = re.compile(r"new in the next release \(#(\d+)\)", re.I)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _docs():
    paths = sorted(glob.glob(os.path.join(REPO, "docs", "*.md"))) + [os.path.join(REPO, "SECURITY.md")]
    return {os.path.relpath(p, REPO).replace(os.sep, "/"): _read(p) for p in paths}


def _unreleased_entries():
    text = _read(os.path.join(REPO, "CHANGELOG.md"))
    m = re.search(r"^## Unreleased\n(.*?)(?=^## )", text, re.M | re.S)
    assert m, "CHANGELOG.md has no '## Unreleased' section followed by a release"
    return [e for e in re.split(r"\n(?=- )", m.group(1)) if e.startswith("- ")]


def _pr_numbers(entry):
    return set(re.findall(r"#(\d+)", entry))


def test_markers_name_unreleased_prs():
    unreleased = set().union(*map(_pr_numbers, _unreleased_entries()))
    stale = [f"{doc}: #{n}" for doc, text in _docs().items()
             for n in MARKER.findall(text) if n not in unreleased]
    assert not stale, ("'new in the next release' markers for PRs not under "
                       f"CHANGELOG Unreleased (released? remove the marker): {stale}")


def test_unreleased_flags_are_marked():
    missing = []
    for entry in _unreleased_entries():
        prs = _pr_numbers(entry)
        for flag in re.findall(r"the new `(--[a-z][a-z0-9-]*)", entry):
            used = re.compile(re.escape(flag) + r"(?![\w-])")
            for doc, text in _docs().items():
                if used.search(text) and not prs & set(MARKER.findall(text)):
                    missing.append(f"{doc}: {flag} (#{', #'.join(sorted(prs))})")
    assert not missing, f"unreleased flags documented without a marker: {missing}"


def test_unreleased_print_feature_is_marked():
    """The entries this guard was written for (#254): --print, prints=True
    and the one-line fallback note are marked where they are described."""
    if not any("#254" in e for e in _unreleased_entries()):
        return
    usage = _read(os.path.join(REPO, "docs", "USAGE.md"))
    api = _read(os.path.join(REPO, "docs", "API.md"))
    print_section = usage.split("### Print output", 1)[1].split("\n#", 1)[0]
    assert MARKER.search(print_section)
    note = usage.split("cannot run a program", 1)[1][:300]
    assert MARKER.search(note)
    for row in ("| `prints` | `False` |", "| `prints` | `list[str]` |", "| `--print FILE` |"):
        lines = [l for l in api.splitlines() if l.startswith(row)]
        assert lines and all(MARKER.search(l) for l in lines), row


def test_readme_has_released_features_only():
    assert not MARKER.search(_read(os.path.join(REPO, "README.md")))
