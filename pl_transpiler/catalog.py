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

"""catalog.py — a small, documented query API over the distilled EL keyword catalogs.

This is the redistributable replacement for the copyrighted MultiCharts reference
corpus (this private preview distribution includes that corpus in the repository
tree, but it is excluded from public releases and from the installable wheel). It
exposes two shipped,
machine-readable data files as a clean query surface:

  * ``pl_transpiler/tools/pl_signatures.jsonl`` — one JSON object per line, each with
    ``keyword``, ``returns``, ``min_arity``, and ``zero_arg_property``.
  * ``pl_transpiler/tools/mc_verified_builtins.txt`` — one token per line (blank lines
    and ``#`` comment lines ignored; a trailing ``# pdf:<line>`` citation is stripped),
    listing builtins manually confirmed to have a real keyword-reference entry.

Both files are read via ``importlib.resources`` so the API works identically from an
installed wheel and from a source checkout. All counts are DERIVED from the data at
load time — nothing is hardcoded.

Library usage::

    from pl_transpiler import catalog
    catalog.get("AverageFC")            # -> record dict (case-insensitive) or None
    catalog.arity("MaxBarsBack")        # -> (min_arity, zero_arg_property)
    catalog.returns("AverageFC")        # -> "numeric"
    catalog.is_verified("datetime")     # -> True
    catalog.search("date")              # -> sorted list of matching keyword names
    catalog.coverage()                  # -> {'signatures': N, 'verified_builtins': M, ...}

CLI::

    python -m pl_transpiler.catalog <name>
    python -m pl_transpiler.catalog --search <substring>
    python -m pl_transpiler.catalog --coverage
"""
import json
import sys
from importlib import resources

_TOOLS_PACKAGE = "pl_transpiler.tools"
_SIGNATURES_RESOURCE = "pl_signatures.jsonl"
_BUILTINS_RESOURCE = "mc_verified_builtins.txt"

# Lazily populated caches (keyed by lower-cased name for case-insensitive lookup).
_signatures = None          # dict: lower-name -> record dict (record['keyword'] is original case)
_verified_builtins = None   # frozenset of lower-cased verified builtin tokens


def _read_resource(name):
    """Read a shipped data file as text, from a wheel or a checkout."""
    return resources.files(_TOOLS_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def _load_signatures():
    global _signatures
    if _signatures is None:
        recs = {}
        for line in _read_resource(_SIGNATURES_RESOURCE).splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            recs[rec["keyword"].lower()] = rec
        _signatures = recs
    return _signatures


def _load_verified_builtins():
    global _verified_builtins
    if _verified_builtins is None:
        toks = set()
        for line in _read_resource(_BUILTINS_RESOURCE).splitlines():
            # Match mc_ground_check.py's parse: text before the first '#', stripped.
            tok = line.split("#", 1)[0].strip().lower()
            if tok:
                toks.add(tok)
        _verified_builtins = frozenset(toks)
    return _verified_builtins


def get(name):
    """Return the signature record dict for ``name`` (case-insensitive), or None.

    The returned dict is a copy of the shipped record augmented with a derived
    ``verified`` bool (True iff the keyword is on the MC-verified-builtin list)."""
    rec = _load_signatures().get(name.lower())
    if rec is None:
        return None
    out = dict(rec)
    out["verified"] = name.lower() in _load_verified_builtins()
    return out


def arity(name):
    """Return ``(min_arity, zero_arg_property)`` for ``name`` (case-insensitive).

    Raises KeyError with a helpful message if the keyword is not in the signature
    catalog."""
    rec = _load_signatures().get(name.lower())
    if rec is None:
        raise KeyError(
            f"{name!r} is not in the EL signature catalog "
            f"({len(_load_signatures())} keywords). Try catalog.search(...) "
            f"to find a similar name."
        )
    return (rec["min_arity"], rec["zero_arg_property"])


def returns(name):
    """Return the declared return type of ``name`` (case-insensitive).

    Raises KeyError with a helpful message if the keyword is not in the catalog."""
    rec = _load_signatures().get(name.lower())
    if rec is None:
        raise KeyError(
            f"{name!r} is not in the EL signature catalog "
            f"({len(_load_signatures())} keywords). Try catalog.search(...) "
            f"to find a similar name."
        )
    return rec["returns"]


def is_verified(name):
    """True iff ``name`` (case-insensitive) is an MC-verified builtin."""
    return name.lower() in _load_verified_builtins()


def search(substring):
    """Return the sorted list of keyword names containing ``substring`` (case-insensitive)."""
    needle = substring.lower()
    sigs = _load_signatures()
    return sorted(rec["keyword"] for low, rec in sigs.items() if needle in low)


def coverage():
    """Return derived catalog counts.

    Keys:
      * ``signatures`` — number of keyword signature records.
      * ``verified_builtins`` — number of MC-verified builtin tokens.
      * ``verified_with_signature`` — verified builtins that also have a signature record.
    All values are computed from the data files, never hardcoded."""
    sigs = _load_signatures()
    verified = _load_verified_builtins()
    return {
        "signatures": len(sigs),
        "verified_builtins": len(verified),
        "verified_with_signature": len(verified & set(sigs.keys())),
    }


def _main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m pl_transpiler.catalog",
        description="Query the distilled EL keyword catalog.",
    )
    ap.add_argument("name", nargs="?", help="keyword to look up (prints its record)")
    ap.add_argument("--search", metavar="SUB", help="list keywords containing SUB")
    ap.add_argument("--coverage", action="store_true", help="print derived catalog counts")
    args = ap.parse_args(argv)

    if args.coverage:
        cov = coverage()
        print(f"signatures: {cov['signatures']}")
        print(f"verified_builtins: {cov['verified_builtins']}")
        print(f"verified_with_signature: {cov['verified_with_signature']}")
        return 0

    if args.search is not None:
        names = search(args.search)
        for n in names:
            print(n)
        print(f"({len(names)} match{'' if len(names) == 1 else 'es'})")
        return 0

    if args.name:
        rec = get(args.name)
        if rec is None:
            print(f"{args.name!r} not found in the signature catalog.", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_main())
