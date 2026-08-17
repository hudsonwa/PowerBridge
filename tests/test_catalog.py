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

"""test_catalog.py — portable (no captures) tests for pl_transpiler.catalog.

Exercises every public API function against real keywords actually present in the
shipped data files, and asserts coverage() equals counts read INDEPENDENTLY from
those data files (re-parsed here, not trusting the module's own loaders)."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import catalog

_SIG_PATH = os.path.join(REPO, "pl_transpiler", "tools", "pl_signatures.jsonl")
_BUILTINS_PATH = os.path.join(REPO, "pl_transpiler", "tools", "mc_verified_builtins.txt")


def _independent_signatures():
    """Re-parse the jsonl independently: lower-name -> record."""
    recs = {}
    with open(_SIG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            recs[rec["keyword"].lower()] = rec
    return recs


def _independent_builtins():
    """Re-parse the builtin token list independently (strip inline '# ...' citation)."""
    toks = set()
    with open(_BUILTINS_PATH, encoding="utf-8") as f:
        for line in f:
            tok = line.split("#", 1)[0].strip().lower()
            if tok:
                toks.add(tok)
    return toks


def test_coverage_matches_data_files():
    sigs = _independent_signatures()
    builtins = _independent_builtins()
    cov = catalog.coverage()
    assert cov["signatures"] == len(sigs), (cov["signatures"], len(sigs))
    assert cov["verified_builtins"] == len(builtins), (cov["verified_builtins"], len(builtins))
    assert cov["verified_with_signature"] == len(builtins & set(sigs.keys())), cov
    # Ground the specific numbers the charter states.
    assert cov["signatures"] == 481, cov
    assert cov["verified_builtins"] == 190, cov


def test_get_case_insensitive():
    sigs = _independent_signatures()
    # Pick a real keyword straight from the data file (original case).
    name = next(json.loads(l)["keyword"] for l in open(_SIG_PATH, encoding="utf-8") if l.strip())
    rec = catalog.get(name)
    assert rec is not None
    assert rec["keyword"].lower() == name.lower()
    # Case-insensitive: upper and lower forms yield the same underlying record.
    assert catalog.get(name.upper())["keyword"] == rec["keyword"]
    assert catalog.get(name.lower())["keyword"] == rec["keyword"]
    # The augmented 'verified' flag agrees with is_verified().
    assert rec["verified"] == catalog.is_verified(name)


def test_arity_and_returns_normal_keyword():
    # DayFromDateTime: a real, verified, single-arg numeric keyword.
    assert catalog.arity("DayFromDateTime") == (1, False)
    assert catalog.arity("dayfromdatetime") == (1, False)  # case-insensitive
    assert catalog.returns("DayFromDateTime") == "numeric"


def test_zero_arg_property_reports_true():
    # BarInterval is a verified 0-arg PROPERTY (called without parentheses in EL).
    min_arity, zero_arg = catalog.arity("BarInterval")
    assert min_arity == 0
    assert zero_arg is True
    rec = catalog.get("BarInterval")
    assert rec["zero_arg_property"] is True


def test_is_verified():
    # 'datetime' is on the verified-builtin list (a reserved word WITHOUT a signature,
    # so it is verified but get() returns None — proving the two catalogs are distinct).
    assert catalog.is_verified("datetime") is True
    assert catalog.is_verified("DATETIME") is True  # case-insensitive
    assert catalog.get("datetime") is None
    # A made-up token is not verified.
    assert catalog.is_verified("definitely_not_a_real_keyword_zzz") is False


def test_search_sorted_and_real():
    names = catalog.search("date")
    assert names == sorted(names), "search must return a sorted list"
    assert len(names) > 0
    # Every result genuinely contains the substring (case-insensitive).
    assert all("date" in n.lower() for n in names)
    # A real known match is present.
    assert any(n.lower() == "currentdate" for n in names)
    # Empty substring matches everything (all signatures).
    assert len(catalog.search("")) == catalog.coverage()["signatures"]


def test_unknown_names_clean_behaviour():
    assert catalog.get("no_such_keyword_xyz") is None
    for fn in (catalog.arity, catalog.returns):
        try:
            fn("no_such_keyword_xyz")
        except KeyError as e:
            assert "signature catalog" in str(e), str(e)
        else:
            raise AssertionError(f"{fn.__name__} must raise KeyError on unknown name")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  [PASS] {t.__name__}")
    print(f"\ntest_catalog: {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
