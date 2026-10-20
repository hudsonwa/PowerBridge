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

"""verify_roundtrip.py — BINDING round-trip gate  (GOAL R2a; harness FIRST).

Built BEFORE any emitter exists, and proven able to go RED. This is the gate that
makes "the emitter round-trips" mean something: for every GT label it takes the
ORIGINAL EL source, runs it through parse -> emit -> a fresh EL file, then reruns
the ENTIRE `verify_all` GT comparison against that emitted file. If the emitter is
faithful, every label stays green; if it corrupts anything, labels go red.

  original EL  --parse-->  AST  --emit-->  EL'  --(ground_truth/.roundtrip/<label>.txt)-->
      run_gt.GT_FILES[label] + golden_diff.GT_FILES[label] repointed at EL'  -->
      verify_all.run_gt_checks(max_bars, scratch)   [VERBATIM — zero new comparison logic]

The real emitter (R2b, `transpiler/codegen_el.py: emit_el(ast)`) is now in place
and takes the AST ONLY — no source text. This gate drives that live emitter: a
green run means every emitted label reproduces its capture; a corrupting emitter
would redden labels. The plumbing (repoint + rerun) is only meaningful alongside
the negative control below, which proves the gate can actually go RED.

NEGATIVE CONTROL (`--self-test`): the whole point of a gate is that it can fail.
A corrupting stub perturbs the emitted EL; the harness then requires EVERY label to
go RED. If any label stays green under corruption the harness is not actually
binding and MUST NOT be trusted. NOTE: the corruption is NOT "flip the first digit
run in the file" — in every GT source the first digit runs live inside the `{...}`
comment header (inert), and single input defaults (e.g. GT1 `Len(14)`) are OVERRIDDEN
per-bar by run_gt from the capture, so a one-literal flip can leave a label green.
Instead we tokenise (the lexer strips comments) and perturb EVERY real NUMBER token
in executable code — guaranteeing at least one compared, strategy-computed column
changes for every label while keeping the source parseable (value change only, no
syntax change; any resulting runtime error is caught by run_gt's throw sidecar ->
FAIL-LOUD, which is itself a RED).

CLI modes (exactly one path per run):
  (default)          full round-trip run; exit 0 iff every label green.
  --self-test        NEGATIVE CONTROL; exit 0 iff every label goes RED.
  --full-range       repoint at emitted files, then run
                     verify_all.run_order_fullrange_checks against them (Rfull
                     plumbing; reuses the exact same monkeypatch). Exit 0 iff green.
  --fixpoint         reparse-fixpoint: parse(emit_el(parse(src))) == parse(src)
                     modulo _line/_col/_warnings/_symbol, over every source
                     (structural only, no per-bar run).
  --mutation-reflect mutate ONE numeric AST literal, emit, forward-run; the output
                     must DIFFER (proves emission derives from the AST, not text).

Usage:
  python3 tests/verify_roundtrip.py
  python3 tests/verify_roundtrip.py --self-test
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests import run_gt
from tests import capture_gate
# run_gt_checks/run_order_fullrange_checks are imported VERBATIM: this gate adds
# ZERO comparison logic (they own the deferral / warmup / throw-sidecar policy;
# re-implementing any of it is how a laxer rule sneaks in).
from tests.verify_all import (
    GT_EXPECT, DEFAULT_GT_BARS, run_gt_checks, run_order_fullrange_checks,
)
# Shared scratch-dir + GT_FILES repoint plumbing (unified across the reverse gates).
from tests.repoint import new_scratch, repoint_files, emit_texts
from pl_transpiler.lexer import tokenise
from pl_transpiler.parser import parse
from pl_transpiler.codegen_el import emit_el

# Emitted EL is written here; run_gt resolves a source name against run_gt.GT
# (ground_truth/), so the patched source name is RELATIVE to that dir.
ROUNDTRIP_DIRNAME = ".roundtrip"


def build_ast(source_text):
    """parse step of the round-trip: EL source -> dict AST (the shared IR)."""
    tokens, positions = tokenise(source_text)
    return parse(tokens, positions=positions, source=source_text)


# R2b: the real emitter is transpiler/codegen_el.py:emit_el(ast). It takes NO
# source text — the round-trip below emits purely from the AST. (The R2a
# passthrough stub is gone; the negative-control corrupt stub below stays, as it
# is what proves this gate can still go RED.)


# --- negative-control corruption -------------------------------------------
def _line_offsets(src):
    """Prefix char-offset of each 1-based line, matching lexer._line_col."""
    offs = []
    acc = 0
    for ln in src.split("\n"):
        offs.append(acc)
        acc += len(ln) + 1
    return offs


def _to_offset(line_offsets, line, col):
    # Inverse of lexer._line_col: offset = start_of_line + (col - 1).
    return line_offsets[line - 1] + (col - 1)


def _mutate_literal(s):
    """Perturb a numeric literal to a DIFFERENT value, staying a valid number.

    +1 is deliberately small: it guarantees a value change (so output differs)
    without turning loop bounds / array sizes into runaway values."""
    if "." in s:
        return repr(float(s) + 1.0)
    return str(int(s) + 1)


def emit_corrupt_stub(ast, source_text):
    """Corrupting stub for the negative control. Perturbs EVERY NUMBER token in
    executable code (the lexer already excluded comments and string bodies), so
    every label is guaranteed at least one changed compared column. Splices from
    the end so earlier offsets stay valid; asserts it changed something."""
    tokens, positions = tokenise(source_text)
    offs = _line_offsets(source_text)
    edits = []  # (start, end, replacement)
    for (ttype, tval), (line, col) in zip(tokens, positions):
        if ttype != "NUMBER":
            continue
        start = _to_offset(offs, line, col)
        end = start + len(tval)
        # Sanity: the located span must actually be the literal we tokenised.
        assert source_text[start:end] == tval, (
            f"number token {tval!r} not at computed offset "
            f"{start}:{end} (found {source_text[start:end]!r})")
        edits.append((start, end, _mutate_literal(tval)))
    if not edits:
        raise RuntimeError(
            "negative control found NO numeric literals to perturb — a corruption "
            "that cannot change output cannot redden the label; harness is broken.")
    out = source_text
    for start, end, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + rep + out[end:]
    return out


# --- round-trip plumbing ----------------------------------------------------
# The two emitters the round-trip drives: the real AST-only emitter (faithful) and
# the negative-control corruptor. Both take ONLY the original source text so they
# plug straight into repoint.emit_texts.
def _emit_faithful(source_text):
    return emit_el(build_ast(source_text))


def _emit_corrupt(source_text):
    return emit_corrupt_stub(build_ast(source_text), source_text)


def run_default(max_bars, scratch):
    """Passthrough round-trip: exit 0 iff every label green."""
    texts = emit_texts(_emit_faithful, list(GT_EXPECT.keys()))
    with repoint_files(ROUNDTRIP_DIRNAME, texts) as labels:
        print("=" * 70)
        print(f"ROUND-TRIP GATE (parse->emit->rerun)  labels={len(labels)}  "
              f"max_bars={max_bars}")
        print("  emitter = pl_transpiler.codegen_el.emit_el (AST-only; R2b)")
        print("=" * 70)
        results = run_gt_checks(max_bars, scratch)
    all_ok = True
    for label, ok, detail, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:10s} {detail}")
        all_ok = all_ok and ok
    print("-" * 70)
    print(f"OVERALL: {'ALL GREEN' if all_ok else 'RED'}  "
          f"(round-trip {'faithful' if all_ok else 'BROKEN'})")
    return 0 if all_ok else 1


def run_self_test(max_bars, scratch):
    """NEGATIVE CONTROL: corrupt the emitted EL; exit 0 iff EVERY label goes RED."""
    texts = emit_texts(_emit_corrupt, list(GT_EXPECT.keys()))
    with repoint_files(ROUNDTRIP_DIRNAME, texts) as labels:
        print("=" * 70)
        print(f"ROUND-TRIP NEGATIVE CONTROL  labels={len(labels)}  max_bars={max_bars}")
        print("  emitter = emit_corrupt_stub (perturbs every NUMBER token in code)")
        print("  PASS criterion: EVERY label must go RED (a gate that can't fail is broken)")
        print("=" * 70)
        results = run_gt_checks(max_bars, scratch)
    all_red = True
    for label, ok, detail, _ in results:
        # Under corruption, RED (ok False) is the desired outcome.
        red = not ok
        print(f"  [{'RED ' if red else 'GREEN'}] {label:10s} {detail}")
        all_red = all_red and red
    print("-" * 70)
    if all_red:
        print("OVERALL: every label RED under corruption -- harness is BINDING.")
        return 0
    still_green = [lbl for lbl, ok, _, _ in results if ok]
    print(f"OVERALL: BROKEN -- labels stayed GREEN under corruption: {still_green}")
    return 1


def run_full_range(max_bars, scratch):
    """Rfull plumbing: repoint at emitted files, then run the full-range order-column
    gate against them (reuses the exact same monkeypatch as the round-trip run)."""
    texts = emit_texts(_emit_faithful, list(GT_EXPECT.keys()))
    with repoint_files(ROUNDTRIP_DIRNAME, texts) as labels:
        print("=" * 70)
        print(f"ROUND-TRIP FULL-RANGE ORDER GATE  labels={len(labels)}  "
              f"max_bars={'ALL' if max_bars is None else max_bars}")
        print("  emitter = pl_transpiler.codegen_el.emit_el (AST-only; R2b); check = "
              "verify_all.run_order_fullrange_checks")
        print("=" * 70)
        results = run_order_fullrange_checks(scratch, max_bars=max_bars)
    all_ok = True
    for label, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:10s} {detail}")
        all_ok = all_ok and ok
    print("-" * 70)
    print(f"OVERALL: {'ALL GREEN' if all_ok else 'RED'}")
    return 0 if all_ok else 1


# --- R2b: reparse-fixpoint --------------------------------------------------
# Keys the parser stamps for diagnostics / retained-symbol provenance that are
# NOT part of the structural AST and are therefore stripped before comparison.
# `_comments` (Rcmt) is the source-comment side-list attached to the program node —
# readability metadata, not structure — so it is stripped here too (also inherited by
# verify_twoway, which reuses _strip).
_FIXPOINT_IGNORE = {"_line", "_col", "_warnings", "_symbol", "_comments"}


def _strip(node):
    """Deep copy of an AST with the non-structural keys removed, for fixpoint."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in _FIXPOINT_IGNORE}
    if isinstance(node, list):
        return [_strip(x) for x in node]
    return node


def _first_divergence(a, b, path="ast"):
    """Return a human string for the first structural divergence, or None."""
    if isinstance(a, dict) and isinstance(b, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            return (f"{path}: key set differs — only-in-A={sorted(ka - kb)} "
                    f"only-in-B={sorted(kb - ka)}")
        for k in a:
            d = _first_divergence(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: list length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_divergence(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return f"{path}: {a!r} != {b!r}"
    return None


def _fixpoint_sources():
    """Every GT_EXPECT source file + every ground_truth/matrix/*.txt (each once,
    by resolved path)."""
    import glob
    paths = []
    seen = set()
    for label in GT_EXPECT:
        p = os.path.join(run_gt.GT, run_gt.GT_FILES[label][0])
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            paths.append(p)
    for p in sorted(glob.glob(os.path.join(run_gt.GT, "matrix", "*.txt"))):
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            paths.append(p)
    return paths


def run_fixpoint():
    """REPARSE-FIXPOINT: parse(emit_el(parse(src))) must structurally equal
    parse(src) modulo _line/_col/_warnings/_symbol, for every fixpoint source.
    Exit 0 iff all hold; prints the first divergent path on any failure."""
    sources = _fixpoint_sources()
    print("=" * 70)
    print(f"REPARSE-FIXPOINT  sources={len(sources)}")
    print("  parse(emit_el(parse(src))) == parse(src)  "
          "(modulo _line/_col/_warnings/_symbol)")
    print("=" * 70)
    all_ok = True
    for path in sources:
        rel = os.path.relpath(path, run_gt.GT)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        ast1 = _strip(build_ast(src))
        try:
            emitted = emit_el(build_ast(src))
            ast2 = _strip(build_ast(emitted))
        except Exception as e:  # emit or reparse blew up -> RED, fail-loud
            print(f"  [FAIL] {rel:40s} {type(e).__name__}: {e}")
            all_ok = False
            continue
        div = _first_divergence(ast1, ast2)
        if div is None:
            print(f"  [PASS] {rel:40s} fixpoint holds")
        else:
            print(f"  [FAIL] {rel:40s} {div}")
            all_ok = False
    print("-" * 70)
    print(f"OVERALL: {'ALL GREEN' if all_ok else 'RED'}")
    return 0 if all_ok else 1


# --- R2b: mutation-reflect --------------------------------------------------
_DECL_TYPES = {"input_decl", "var_decl", "array_decl"}


def _number_refs(node, in_decl=False, out=None):
    """Pre-order list of (number_node, in_declaration) for every 'number' node.

    `in_declaration` flags literals that are Input/Var/Array defaults — run_gt
    OVERRIDES input defaults per-bar from the capture (and var/array inits are
    typically reassigned), so those are inert and must be tried LAST."""
    if out is None:
        out = []
    if isinstance(node, dict):
        if node.get('type') == 'number':
            out.append((node, in_decl))
        child_decl = in_decl or node.get('type') in _DECL_TYPES
        for v in node.values():
            _number_refs(v, child_decl, out)
    elif isinstance(node, list):
        for x in node:
            _number_refs(x, in_decl, out)
    return out


def run_mutation_reflect(scratch):
    """MUTATION-REFLECT: proves emission derives from the AST, not smuggled source
    text. For a GT source: emit as-is and run; then mutate ONE numeric literal
    node in the AST, emit, run; assert the two forward-run outputs DIFFER. If they
    were identical the emitter would be ignoring the AST it was handed."""
    # A calc-only label with plentiful literals; small bounded window is enough.
    label = "GT1"
    max_bars = 400
    warmup = 300
    orig_src_name = run_gt.GT_FILES[label][0]
    with open(os.path.join(run_gt.GT, orig_src_name), encoding="utf-8") as f:
        src = f.read()

    print("=" * 70)
    print(f"MUTATION-REFLECT  label={label}  max_bars={max_bars}")
    print("  mutate ONE 'number' node in the AST -> emit -> forward-run must DIFFER")
    print("=" * 70)

    def _emit_run(ast, tag):
        emitted = emit_el(ast)
        csv_path = os.path.join(scratch, f"mut_{label.lower()}_{tag}.csv")
        # Same save/restore repoint as everywhere else, tagged per attempt so the
        # base/mut emitted files never overwrite each other.
        with repoint_files(ROUNDTRIP_DIRNAME, {label: emitted},
                           name=lambda lbl, t=tag: f"{lbl}_{t}.txt"):
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                run_gt.run_gt(label, max_bars=max_bars, output_path=csv_path,
                              warmup=warmup)
        with open(csv_path, encoding="utf-8") as f:
            return f.read()

    base_out = _emit_run(build_ast(src), "base")

    # Pre-order index every numeric literal; try executable (non-declaration)
    # literals FIRST (input defaults are overridden per-bar, so inert). Each
    # attempt mutates EXACTLY ONE literal node in a FRESH AST, emits, and runs.
    catalogue = _number_refs(build_ast(src))
    order = ([i for i, (_, d) in enumerate(catalogue) if not d] +
             [i for i, (_, d) in enumerate(catalogue) if d])
    print(f"  {len(catalogue)} numeric literals "
          f"({sum(1 for _, d in catalogue if not d)} executable, "
          f"{sum(1 for _, d in catalogue if d)} declaration-default)")

    for i in order:
        mut_ast = build_ast(src)               # fresh AST each attempt
        num = _number_refs(mut_ast)[i][0]      # same pre-order slot
        old = num["value"]
        num["value"] = "13" if old != "13" else "17"
        num.pop("_symbol", None)               # keep it a plain number literal
        mut_out = _emit_run(mut_ast, "mut")
        if mut_out != base_out:
            print(f"  mutated literal #{i}: {old!r} -> {num['value']!r} "
                  f"(in_decl={catalogue[i][1]})")
            print("-" * 70)
            print("OVERALL: a single-literal AST mutation CHANGED the forward-run "
                  "output -- emission reflects the AST, not smuggled source text.")
            return 0

    print("-" * 70)
    print("OVERALL: RED -- NO single-literal AST mutation changed the output; "
          "emission is not AST-derived.")
    return 1


def main():
    ap = argparse.ArgumentParser(description="Round-trip binding gate (GOAL R2a)")
    ap.add_argument("--self-test", action="store_true",
                    help="NEGATIVE CONTROL: corrupt the emitted EL; pass iff every "
                         "label goes RED.")
    ap.add_argument("--full-range", action="store_true",
                    help="repoint at emitted files, run the full-range order-column "
                         "gate against them (Rfull plumbing).")
    ap.add_argument("--fixpoint", action="store_true",
                    help="reparse-fixpoint: parse(emit_el(parse(src))) == parse(src) "
                         "modulo _line/_col/_warnings/_symbol, over every GT + matrix "
                         "source. Structural only (no per-bar run).")
    ap.add_argument("--mutation-reflect", action="store_true", dest="mutation_reflect",
                    help="mutate ONE numeric AST literal, emit, forward-run; the "
                         "output must DIFFER (proves emission derives from the AST).")
    ap.add_argument("--max-bars", type=int, default=None, dest="max_bars",
                    help="GT bars after warmup. Default: verify_all.DEFAULT_GT_BARS "
                         "for round-trip/self-test (the same bounded window verify_all "
                         "uses); true full range for --full-range.")
    ap.add_argument("--scratch", default=None,
                    help="predicted-CSV working dir. Default: a unique per-run dir "
                         "under .scratch/ (auto-removed) so concurrent runs never "
                         "collide on the same file.")
    args = ap.parse_args()

    if args.fixpoint:
        # Structural check only; no per-bar run, so no captures / scratch needed.
        return run_fixpoint()

    # Every OTHER mode reruns the per-bar GT comparison, which needs the pinned
    # MultiCharts captures. In a captures-less clone SKIP (one-line docs pointer)
    # and return success so the suite stays green; when present, run as before.
    if not capture_gate.captures_present():
        print(capture_gate.skip_message("ROUNDTRIP gate"))
        return 0

    scratch = args.scratch or new_scratch("rt-")

    if args.mutation_reflect:
        return run_mutation_reflect(scratch)

    if args.full_range:
        # true full range unless overridden (a value is only for fast self-test)
        return run_full_range(args.max_bars, scratch)

    max_bars = DEFAULT_GT_BARS if args.max_bars is None else args.max_bars
    if args.self_test:
        return run_self_test(max_bars, scratch)
    return run_default(max_bars, scratch)


if __name__ == "__main__":
    sys.exit(main())
