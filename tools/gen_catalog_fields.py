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

"""gen_catalog_fields.py — derive the evidence fields of pl_transpiler/tools/pl_signatures.jsonl.

Each record keeps its original fields (``keyword``, ``returns``, ``min_arity``,
``zero_arg_property``) untouched; this script (re)computes five more, using only
PowerBridge's own code and tests as evidence:

  * ``kind`` — ``syntax`` for language words (the lexer's ``KEYWORDS``, the parser's
    declaration keywords, the order-clause words the semantic pass reserves in
    ``_RESERVED_IDENTS``) and the English filler words listed in ``FILLER_WORDS``;
    ``builtin`` for everything else.
  * ``max_arity`` — the largest accepted argument count, from (first match wins):
    the semantic pass's arity table (``parser_tables.BUILTIN_ARITY``); codegen's
    arity table (``codegen_util._EXTRA_ARITY``); codegen's inline lowerings of
    ``__marker__`` intrinsics, when every lowering tests ``len(args) == N`` (other
    counts are rejected as unimplemented); or the live runtime function's fixed
    positional signature. Runtime functions taking ``*args`` (including the loud
    unimplemented stubs) say nothing about arity, so they give ``null``.
    ``mc_proven.json`` (used by el_emit) lists names only, so it adds no arity.
  * ``arg_types`` — one of numeric/any per argument. ``numeric``
    only where the call lowers to a numeric math intrinsic in codegen; ``any`` per
    argument where the arity is known but PowerBridge does not constrain the type;
    ``null`` where the arity itself is unknown.
  * ``verification`` / ``verified_by``:
      - ``n/a`` — syntax words: verification does not apply; ``verified_by`` is null.
      - ``verified:capture-strategy`` — a builtin used by a ground-truth strategy whose
        per-bar outputs (required columns) tests/verify_all.py compares against a
        recorded MultiCharts capture. This does not mean the keyword's own value was checked, and the
        captures are not public, so that check skips in a clone. ``verified_by``
        names the test and script.
      - ``verified:test`` — EL source embedded in tests/ calls the builtin (``Name(``)
        or, for a zero-argument property, names it. ``verified_by`` is the first such
        test file (sorted).
      - ``documented-unverified`` — everything else; ``verified_by`` is null.

Usage:  python tools/gen_catalog_fields.py [--check]
"""
import ast
import collections
import inspect
import io
import json
import os
import re
import sys
import tokenize

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import codegen_expr  # noqa: E402
from pl_transpiler._codegen_inline_calls import INLINE_CALLS  # noqa: E402
from pl_transpiler.builtins import BUILTINS  # noqa: E402
from pl_transpiler.codegen_util import _EXTRA_ARITY  # noqa: E402
from pl_transpiler.lexer import KEYWORDS  # noqa: E402
from pl_transpiler.parser import _DECL_KEYWORDS  # noqa: E402
from pl_transpiler.parser_tables import BUILTIN_ARITY, BUILTIN_FUNC_MAP, RISK_FUNCS  # noqa: E402
from pl_transpiler.tools import mc_ground_check  # noqa: E402

SIG_PATH = os.path.join(REPO, "pl_transpiler", "tools", "pl_signatures.jsonl")
TESTS = os.path.join(REPO, "tests")
CAPTURE_TEST = "tests/verify_all.py"
CAPTURE_LEVEL = "verified:capture-strategy"
REGEN_CMD = "python tools/gen_catalog_fields.py"

# Order-clause words the semantic pass reserves (semantic._RESERVED_IDENTS, "order-
# quantity / timing words"); they are grammar, not callable builtins.
ORDER_CLAUSE_WORDS = {
    "next", "this", "bar", "at", "market", "on", "shares", "contracts",
    "share", "contract", "total", "stop", "limit",
}
# English filler words EL accepts for readability (``Buy next bar at market``,
# ``crosses above``, ``2 bars ago``); no parser table names them, so they are listed here.
FILLER_WORDS = {
    "a", "an", "ago", "bars", "based", "by", "does", "from", "is", "of", "than", "the",
    "was", "above", "below", "over", "under", "place", "higher", "entry",
}
SYNTAX_WORDS = frozenset(KEYWORDS | _DECL_KEYWORDS | ORDER_CLAUSE_WORDS | FILLER_WORDS)

# Lowerings in parser_tables.BUILTIN_FUNC_MAP that are plain numeric math (each
# argument is converted with float() or fed to a math-module call).
NUMERIC_INTRINSICS = {
    "__abs__", "__ceiling__", "__expvalue__", "__floor__", "__fracportion__",
    "__intportion__", "__log__", "__power__", "__round__", "__sign__", "__square__",
    "__sqrt__", "pl_squareroot",
}

# Files under tests/ that only list catalog names (they would make every keyword
# look exercised) and are therefore not evidence.
_NOT_EVIDENCE = {"test_catalog.py", "coverage_report.py", "test_catalog_fields.py"}
# Frozen el_emit output: a copy of ground_truth/, not independent evidence.
_NOT_EVIDENCE_DIRS = {os.path.join(TESTS, "fixtures", "emitted")}

FIELD_ORDER = ("keyword", "kind", "returns", "min_arity", "zero_arg_property",
               "arg_types", "max_arity", "verification", "verified_by")


def _fixed_positional(fn):
    """Number of fixed positional params, or None if the function takes *args/**kwargs."""
    params = inspect.signature(fn).parameters.values()
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params):
        return None
    return sum(1 for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))


# Probe lengths for the inline-expansion table; an entry that also accepts the
# largest one has no exact count test (no test, or a ``>=`` test).
_PROBE_ARITIES = range(9)


def _inline_call_arities():
    """{marker: accepted arg counts, or None if not exact} for INLINE_CALLS entries.

    Each entry returns None for an arg count it has no expansion for, so probing
    with digit-string args finds the exact ``len(args) == N`` counts."""
    found = {}
    for marker, expand in INLINE_CALLS.items():
        ok = []
        for n in _PROBE_ARITIES:
            try:
                accepted = expand(marker, [str(i + 1) for i in range(n)]) is not None
            except (IndexError, ValueError):
                accepted = False
            if accepted:
                ok.append(n)
        found[marker] = ok if ok and _PROBE_ARITIES[-1] not in ok else None
    return found


def _marker_arities():
    """{marker: max N} for ``__marker__`` intrinsics that codegen lowers only for
    exact arg counts: ``py_name == '__marker__' and len(args) == N`` tests in
    codegen_expr, or INLINE_CALLS entries that expand only for exact counts. A
    marker that appears in any other form (no count test, ``>=``, a membership
    test) is left out: unknown."""
    tree = ast.parse(inspect.getsource(codegen_expr))
    exact = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And)
                and len(node.values) == 2):
            continue
        a, b = node.values
        if not (isinstance(a, ast.Compare) and isinstance(a.ops[0], ast.Eq)
                and isinstance(a.left, ast.Name) and a.left.id == "py_name"
                and isinstance(a.comparators[0], ast.Constant)):
            continue
        marker = a.comparators[0].value
        if (isinstance(b, ast.Compare) and isinstance(b.ops[0], ast.Eq)
                and isinstance(b.left, ast.Call) and getattr(b.left.func, "id", "") == "len"
                and isinstance(b.comparators[0], ast.Constant)):
            exact.setdefault(marker, []).append(b.comparators[0].value)
    inline = _inline_call_arities()
    # Keep only markers whose every occurrence is one of the exact tests counted above.
    counts = collections.Counter(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and (n.value in exact or n.value in inline))
    out = {m: max(ns) for m, ns in exact.items()
           if counts[m] == len(ns) and m not in inline}
    for m, ns in inline.items():
        if ns is not None and counts[m] == len(exact.get(m, ())):
            out[m] = max(ns + exact.get(m, []))
    return out


_MARKER_ARITY = _marker_arities()


def derive_max_arity(low):
    if low in BUILTIN_ARITY:
        return max(BUILTIN_ARITY[low])
    if low in _EXTRA_ARITY:
        return _EXTRA_ARITY[low][1]
    marker = BUILTIN_FUNC_MAP.get(low)
    if low in RISK_FUNCS:
        return None  # parsed as a statement by the parser; the expression lowering never sees it
    if marker in _MARKER_ARITY:
        return _MARKER_ARITY[marker]
    if marker and marker.startswith("__"):
        return None
    fn = BUILTINS.get(low)
    if fn is not None:
        return _fixed_positional(fn)
    return None


def derive_arg_types(low, max_arity):
    if max_arity is None:
        return None
    if BUILTIN_FUNC_MAP.get(low) in NUMERIC_INTRINSICS:
        return ["numeric"] * max_arity
    return ["any"] * max_arity


_EL_STMT_END = re.compile(r";([ \t]*\n|\Z)")
_STMT_START = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)


def _python_strings(text):
    """EL-looking string literals of a Python file (where tests embed their EL source).

    Docstrings and other bare-statement strings are skipped, and so is any literal
    without a line ending in ``;`` (as EL statements do), so prose never counts as usage."""
    out = []
    prev = tokenize.NEWLINE
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.STRING and prev not in _STMT_START:
                try:
                    value = ast.literal_eval(tok.string)
                except (ValueError, SyntaxError):
                    value = None
                if isinstance(value, str) and _EL_STMT_END.search(value):
                    out.append(value)
            if tok.type not in (tokenize.COMMENT, tokenize.NL):
                prev = tok.type
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "\n".join(out)


def test_corpus():
    """[(relative_path, EL-ish text)] for every evidence file under tests/, sorted."""
    corpus = []
    for root, dirs, files in os.walk(TESTS):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__"
                         and os.path.join(root, d) not in _NOT_EVIDENCE_DIRS)
        for name in sorted(files):
            if name in _NOT_EVIDENCE or not name.endswith((".py", ".el", ".txt", ".json")):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            if name.endswith(".py"):
                text = _python_strings(text)
            # Drop EL comments and EL string literals: only code counts as usage.
            text = mc_ground_check._strip(text)
            corpus.append((os.path.relpath(path, REPO).replace(os.sep, "/"), text))
    corpus.sort()
    return corpus


def capture_corpus():
    """[(source_rel, identifier tokens)] for the GT scripts verify_all compares to captures."""
    from tests.gt_manifest import GT_FILES
    from tests.verify_all import GT_EXPECT
    out = []
    for src in sorted({GT_FILES[label][0] for label in GT_EXPECT if label in GT_FILES}):
        with open(os.path.join(REPO, "ground_truth", src), encoding="utf-8", errors="ignore") as fh:
            code = mc_ground_check._strip(fh.read())
        out.append(("ground_truth/" + src, {w.lower() for w in re.findall(r"[A-Za-z_]\w*", code)}))
    return out


def derive_kind(low):
    return "syntax" if low in SYNTAX_WORDS else "builtin"


def derive_verification(rec, tests, captures):
    low = rec["keyword"].lower()
    if rec["kind"] == "syntax":
        return "n/a", None
    for src, toks in captures:
        if low in toks:
            return CAPTURE_LEVEL, f"{CAPTURE_TEST} ({src})"
    word = re.escape(rec["keyword"])
    pat = r"\b%s\b" % word if rec["zero_arg_property"] else r"\b%s\s*\(" % word
    rx = re.compile(pat, re.IGNORECASE)
    for rel, text in tests:
        if rx.search(text):
            return "verified:test", rel
    return "documented-unverified", None


def build():
    tests, captures = test_corpus(), capture_corpus()
    lines = []
    with open(SIG_PATH, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            low = rec["keyword"].lower()
            rec["kind"] = derive_kind(low)
            if rec["kind"] == "syntax":
                rec["max_arity"] = rec["arg_types"] = None
            else:
                rec["max_arity"] = derive_max_arity(low)
                rec["arg_types"] = derive_arg_types(low, rec["max_arity"])
            rec["verification"], rec["verified_by"] = derive_verification(rec, tests, captures)
            ordered = {k: rec[k] for k in FIELD_ORDER if k in rec}
            ordered.update({k: v for k, v in rec.items() if k not in ordered})
            lines.append(json.dumps(ordered))
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text = build()
    if argv == ["--check"]:
        with open(SIG_PATH, encoding="utf-8") as fh:
            ok = fh.read() == text
        print("pl_signatures.jsonl is up to date" if ok
              else f"pl_signatures.jsonl is stale: run {REGEN_CMD}")
        return 0 if ok else 1
    with open(SIG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"wrote {SIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
