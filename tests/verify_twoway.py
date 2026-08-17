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

"""verify_twoway.py — BINDING two-way gate  (GOAL R4).

Closes the full loop through the *Python* side of the transpiler:

  original EL --parse--> AST1 --LIVE transpile(src, trace=False)--> clean Python
      --py_front.py_to_ast--> AST2 --emit_el--> EL''
      --(ground_truth/.twoway/<label>.txt)--> run_gt.GT_FILES / golden_diff.GT_FILES
      repointed at EL''  -->  verify_all.run_gt_checks(max_bars, scratch)  [VERBATIM]

Two independent assertions per GT_EXPECT label, BOTH required to pass:

  (1) BEHAVIOURAL — EL'' is rerun through the SAME monkeypatch + run_gt_checks
      machinery as verify_roundtrip (helpers imported, never re-implemented: this
      gate owns ZERO comparison / deferral / warmup logic). If the Python front-end
      corrupts anything, columns diverge and the label goes RED.

  (2) STRUCTURAL — AST1 == AST2 after the documented normalisations below, compared
      with verify_roundtrip._first_divergence (again reused — the diff engine is not
      forked). The first divergent path is printed on failure.

Both use the LIVE forward transpiler (`transpile(..., trace=False)`), so a drift in
either direction (codegen changes the dialect, or py_front stops inverting it) reddens
this gate the moment it happens.

STRUCTURAL NORMALISATIONS (the residue the clean dialect provably cannot carry; each
is invisible to behaviour because the forward emitter already collapses it — see
transpiler/py_front.py "INFORMATION THE CLEAN DIALECT CANNOT CARRY"):

  N0. drop the parser's diagnostic keys `_line/_col/_warnings/_symbol`
      (verify_roundtrip._strip).
  N1. drop `intrabar_persist` and `data_ref` from declaration entries — the clean
      dialect encodes neither (IntraBarPersist is a no-op at bar close; a Var's
      DataN ref is dropped by the forward emitter, which reads the local series).
  N2. merge consecutive same-type declaration nodes (input_decl / var_decl /
      array_decl) — the parser groups `Vars: a(0), b(0)` into ONE node; py_front,
      reading one clean-Python assignment per line, emits one node per name.
  N3. drop top-level `attribute` nodes (`[SameExitFromOneEntryOnce = true]` and
      friends) — compile directives the forward transpiler does not emit.
  N4. canonicalise every call `name` through BUILTIN_FUNC_MAP to its emitted py_name
      (the map is many-to-one: atr/avgtruerange, macd/macdvalue, minute/second, ...);
      plus the one emission alias `__eldatetodatetime__` -> `pl_datetojulian`
      (1-arg ELDateToDateTime and DateToJulian emit identically).
  N5. collapse a `bar_ref` whose series is a position_kw / mc_kw to the bare keyword
      — MaxPositionProfit(1) etc. emit `kwargs.get('max_position_profit', 0)`, dropping
      the bars-back index.
  N6. clear the args of EntryName / ExitName calls — the forward emitter drops the
      index arg (returns a fixed `kwargs.get('entry_name', '')`).
  N7. drop the reverse-only surface keys the clean Python dialect cannot carry:
      `_ident` / `_var_ident` (a user identifier's ORIGINAL casing — Python variables
      are lowercase and case-sensitive, so the clean dialect encodes only the folded
      name) and `then_block` / `else_block` / `block` (whether the source wrote an
      explicit Begin/End — the clean dialect has no compact single-statement form, so
      AST2 legitimately lacks the flag). Both are DOCUMENTED normalisations of the
      Python leg; the EL leg still round-trips them (verify_roundtrip does NOT ignore
      them). Only ever present on AST1, so a no-op on AST2.

CLI:
  python3 tests/verify_twoway.py               # exit 0 iff every label green (both parts)
  python3 tests/verify_twoway.py --max-bars N  # override the GT window
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests import run_gt
from tests import capture_gate
# run_gt_checks is imported VERBATIM (it owns the deferral / warmup / throw policy).
from tests.verify_all import GT_EXPECT, DEFAULT_GT_BARS, run_gt_checks
# The parse step, the diagnostic-key strip, and the structural diff are reused from
# the round-trip gate — this gate forks NONE of them.
from tests.verify_roundtrip import build_ast, _strip, _first_divergence
# Shared scratch-dir + GT_FILES repoint plumbing (unified across the reverse gates).
from tests.repoint import new_scratch, repoint_files
from pl_transpiler import transpile
from pl_transpiler import py_front
from pl_transpiler.codegen_el import emit_el
from pl_transpiler.codegen import BUILTIN_FUNC_MAP

# EL'' is written here; run_gt resolves a source name relative to run_gt.GT.
TWOWAY_DIRNAME = ".twoway"

_DECL_TYPES = {"input_decl", "var_decl", "array_decl"}
# N4: many-to-one call aliases collapse to the emitted py_name; the one emission
# alias the map cannot express (1-arg ELDateToDateTime == DateToJulian) is added.
_EMISSION_ALIAS = {"__eldatetodatetime__": "pl_datetojulian"}
# N1: declaration-entry keys the clean dialect cannot carry.
_DROP_DECL_KEYS = {"intrabar_persist", "data_ref"}
# N7: reverse-only surface keys the clean Python dialect cannot carry (original
# identifier casing; explicit Begin/End retention). Present on AST1 only.
_DROP_STRUCT_KEYS = {"_ident", "_var_ident", "then_block", "else_block", "block"}
# N6: calls whose args the forward emitter drops.
_ARGLESS_NAME_CALLS = {"__entryname__", "__exitname__"}


def _canon_call(name):
    py = BUILTIN_FUNC_MAP.get(name, name)
    return _EMISSION_ALIAS.get(py, py)


def _normalise(node):
    """AST1/AST2 -> canonical form for structural comparison. Applies N0 (via the
    reused _strip) then N1-N6. Runs on both sides identically; the normalisations
    that only ever fire on AST1 (grouped decls, attribute nodes, keyword bar_refs)
    are simply no-ops on AST2."""
    return _apply(_strip(node))


def _apply(node):
    if isinstance(node, dict):
        d = {}
        for k, v in node.items():
            if k in _DROP_DECL_KEYS or k in _DROP_STRUCT_KEYS:   # N1 / N7
                continue
            d[k] = _apply(v)
        if d.get("type") == "call" and "name" in d:
            d["name"] = _canon_call(d["name"])   # N4
            if d["name"] in _ARGLESS_NAME_CALLS:  # N6
                d["args"] = []
        if (d.get("type") == "bar_ref" and isinstance(d.get("series"), dict)
                and d["series"].get("type") in ("position_kw", "mc_kw")):  # N5
            return d["series"]
        return d
    if isinstance(node, list):
        kept = [x for x in node
                if not (isinstance(x, dict) and x.get("type") == "attribute")]  # N3
        return _merge_decls([_apply(x) for x in kept])                          # N2
    return node


def _merge_decls(items):
    out = []
    for it in items:
        if (isinstance(it, dict) and it.get("type") in _DECL_TYPES
                and out and isinstance(out[-1], dict)
                and out[-1].get("type") == it.get("type")):
            out[-1] = {**out[-1], "decls": out[-1]["decls"] + it["decls"]}
        else:
            out.append(it)
    return out


def _two_way(source_text):
    """EL -> AST1 ; EL -> clean Python -> AST2 ; AST2 -> EL''. Uses the LIVE forward
    transpiler for the clean Python (a codegen drift reddens the gate immediately)."""
    ast1 = build_ast(source_text)
    clean_py = transpile(source_text, trace=False)
    ast2 = py_front.py_to_ast(clean_py)
    return ast1, ast2, emit_el(ast2)


def _compute_twoway():
    """For every GT_EXPECT label: read the ORIGINAL EL, run the two-way pipeline, and
    compute the structural divergence. Returns (texts, per_label) where
    texts[label] = EL'' ONLY for labels that emitted, and per_label[label] =
    (structural_div_or_None, emit_ok). A per-label try/except keeps the continue-on-
    error semantics — a label whose py_front/emit blows up is recorded RED but does
    not abort the others (and is left OUT of texts, so it is never repointed)."""
    texts = {}
    per_label = {}
    for label in GT_EXPECT:
        orig_src_name = run_gt.GT_FILES[label][0]
        with open(os.path.join(run_gt.GT, orig_src_name), encoding="utf-8") as f:
            source_text = f.read()
        try:
            ast1, ast2, emitted = _two_way(source_text)
        except Exception as e:  # fail-loud: py_front / emit blew up -> RED
            per_label[label] = (f"{type(e).__name__}: {e}", False)
            continue
        per_label[label] = (_first_divergence(_normalise(ast1), _normalise(ast2)), True)
        texts[label] = emitted
    return texts, per_label


def run_default(max_bars, scratch):
    texts, per_label = _compute_twoway()
    labels = list(GT_EXPECT.keys())
    print("=" * 70)
    print(f"TWO-WAY GATE (parse -> transpile -> py_front -> emit -> rerun)  "
          f"labels={len(labels)}  max_bars={max_bars}")
    print("  forward = pl_transpiler.transpile(trace=False) [LIVE]; "
          "inverse = pl_transpiler.py_front; emitter = pl_transpiler.codegen_el.emit_el")
    print("=" * 70)

    # --- structural (AST1 == AST2 modulo N0-N6) ---
    print("STRUCTURAL  (AST1 == AST2, documented normalisations):")
    struct_ok = True
    for label in labels:
        div, emit_ok = per_label[label]
        ok = emit_ok and div is None
        struct_ok = struct_ok and ok
        detail = "match" if ok else (div or "emit failed")
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:10s} {detail}")

    # --- behavioural (reused run_gt_checks against EL'') ---
    print("-" * 70)
    print("BEHAVIOURAL  (run_gt_checks against EL''):")
    with repoint_files(TWOWAY_DIRNAME, texts):
        results = run_gt_checks(max_bars, scratch)
    behav_ok = True
    for label, ok, detail, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:10s} {detail}")
        behav_ok = behav_ok and ok
    # A label that failed to emit never got repointed; force it RED here too.
    for label in labels:
        if not per_label[label][1]:
            behav_ok = False

    print("-" * 70)
    all_ok = struct_ok and behav_ok
    print(f"OVERALL: {'ALL GREEN' if all_ok else 'RED'}  "
          f"(structural {'ok' if struct_ok else 'RED'}, "
          f"behavioural {'ok' if behav_ok else 'RED'})")
    return 0 if all_ok else 1


def main():
    ap = argparse.ArgumentParser(description="Two-way binding gate (GOAL R4)")
    ap.add_argument("--max-bars", type=int, default=None, dest="max_bars",
                    help="GT bars after warmup (default: verify_all.DEFAULT_GT_BARS).")
    ap.add_argument("--scratch", default=None,
                    help="predicted-CSV working dir (default: a unique per-run dir "
                         "under .scratch/, auto-removed).")
    args = ap.parse_args()
    # The behavioural leg reruns the per-bar GT comparison, which needs the pinned
    # MultiCharts captures. In a captures-less clone SKIP and return success; when
    # present, run exactly as before.
    if not capture_gate.captures_present():
        print(capture_gate.skip_message("TWOWAY gate"))
        return 0
    scratch = args.scratch or new_scratch("tw-")
    max_bars = DEFAULT_GT_BARS if args.max_bars is None else args.max_bars
    return run_default(max_bars, scratch)


if __name__ == "__main__":
    sys.exit(main())
