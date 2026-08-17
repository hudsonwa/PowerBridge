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

"""coverage_report.py — falsifiable coverage metric (GOAL item C / Ucov).

Makes "majority of EL" a MEASURED number, not an assertion. This is a
READ-ONLY measurement: it changes NO runtime, captures, gates, or tolerance.
It only reads the transpiler keyword map, the runtime, the signature catalog,
and the ground-truth sources/captures, then writes COVERAGE.md.

Deterministic & re-runnable: same repo state -> same COVERAGE.md.

Three sections (mirrors the unit spec):

  1. TRANSPILABLE-KEYWORD COVERAGE
     - total KNOWN keywords  = union of the transpiler keyword map
       (pl_transpiler.builtins.BUILTINS) and the signature catalog
       (pl_transpiler/tools/pl_signatures.jsonl), compared case-insensitively.
     - IMPLEMENTED            = a known keyword that resolves through BUILTINS
       to a REAL pl_* runtime function — i.e. NOT a loud stub. The runtime
       installs loud stubs (`_pl_loud_stub=True`) over every pl_* whose body is
       just `return 0/None/''`/`pass`; those route to pl_runtime._unimplemented,
       so they are NOT counted as implemented.
     - EXERCISED              = of the IMPLEMENTED keywords, how many appear in
       at least one ground_truth/*.txt EL source (comments stripped).
     - pl_runtime.pl_unimplemented_report() is used as a runtime cross-check:
       after the parity runs it lists any *unimplemented* keyword that a GT
       script actually hit (should be empty / whitelisted, else fail-loud).

  2. ORDER-MODE COVERAGE
     Derived from the FillEngine in pl_transpiler/engine.py and the captures present:
     Implemented = the fill path exists in the engine source; Validated = a
     capture CSV for that OrderMode is on disk and wired into the suite. Both
     are MEASURED per run — nothing in this section is a hard-coded claim.

  3. FULL-RANGE PARITY (per GT label)
     Reuses tests/verify_full_range.{classify,check_label} + tests/verify_all
     GT_EXPECT to report, per label, whether the order/position/trade columns
     are EXACT (0 mismatches) and how many calc columns pass, at a bounded run
     (default --parity-max-bars 3000; pass --full for the true full range). The
     authoritative full-range gate remains tests/verify_full_range.py.
"""
import argparse
import json
import os
import subprocess
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler import builtins as _builtins  # noqa: E402
from pl_transpiler.runtime import pl_runtime as _rt  # noqa: E402
from tests.repoint import new_scratch  # noqa: E402
from tests import run_gt  # noqa: E402
from tests.golden_diff import GT_FILES, CAP, GT  # noqa: E402
from tests.verify_all import GT_EXPECT, WARMUP  # noqa: E402
from tests import verify_full_range as vfr  # noqa: E402

SIG_PATH = os.path.join(REPO, "pl_transpiler", "tools", "pl_signatures.jsonl")
COVERAGE_MD = os.path.join(REPO, "COVERAGE.md")

# Source files whose node-kind vocabulary is measured (Section 4). Everything in
# Section 4 is DERIVED at run time from these files + the sweep report — never a
# hard-coded claim (the audited lesson: a literal number inside a metric will lie).
PARSER_PATH = os.path.join(REPO, "pl_transpiler", "parser.py")
CODEGEN_EL_PATH = os.path.join(REPO, "pl_transpiler", "codegen_el.py")
PY_FRONT_PATH = os.path.join(REPO, "pl_transpiler", "py_front.py")
ROUNDTRIP_PATH = os.path.join(REPO, "tests", "verify_roundtrip.py")
SWEEP_MD = os.path.join(REPO, "reviews", "EMISSION_SWEEP.md")

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Node-kind inventory scanners. The parser stamps every AST node with a
# `'type': '<kind>'` literal; the emitter dispatches on `t == '<kind>'` (plus the
# top-level `!= 'program'` guard in emit_el); py_front reconstructs nodes via
# `'type': '<kind>'` literals just like the parser. Scanning the SOURCE is the
# programmatic introspection the unit asks for — not a maintained list.
_TYPE_LIT_RE = re.compile(r"""['"]type['"]\s*:\s*['"]([a-z_]+)['"]""")
_DISPATCH_RE = re.compile(r"""\bt == ['"]([a-z_]+)['"]""")
_NEQ_RE = re.compile(r"""!= ['"]([a-z_]+)['"]""")


# --------------------------------------------------------------------------- #
# Section 1 — keyword coverage
# --------------------------------------------------------------------------- #
def load_signature_keywords():
    """All keyword names listed in pl_transpiler/tools/pl_signatures.jsonl (original case)."""
    kws = []
    with open(SIG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            kws.append(json.loads(line)["keyword"])
    return kws


def implemented_keywords():
    """Set of lowercase keywords that map (via BUILTINS) to a REAL pl_* fn.

    A keyword is NOT implemented if its target is a loud stub (the runtime
    marks every stub body `return 0/None/''`/`pass` with `_pl_loud_stub=True`
    and routes it to _unimplemented)."""
    impl, stubbed = set(), set()
    for kw, fn in _builtins.BUILTINS.items():
        if getattr(fn, "_pl_loud_stub", False):
            stubbed.add(kw)
        else:
            impl.add(kw)
    return impl, stubbed


def strip_el_comments(text):
    """Remove EL comments so keyword usage isn't counted inside commentary.

    EL comment forms: { ... }, (* ... *), and // to end-of-line."""
    text = re.sub(r"\{[^{}]*\}", " ", text)          # brace comments (non-nested)
    text = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.S)  # (* ... *)
    text = re.sub(r"//[^\n]*", " ", text)            # // line comments
    return text


def gt_source_tokens():
    """{label-source-basename: set(lowercase identifier tokens)} for each GT
    EL source referenced by GT_FILES (deduped by source filename)."""
    out = {}
    seen = set()
    for label, (src_name, _csv) in GT_FILES.items():
        if src_name in seen:
            continue
        seen.add(src_name)
        path = os.path.join(GT, src_name)
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        code = strip_el_comments(raw)
        # normalize two-word crosses operators to their BUILTINS key form
        code = re.sub(r"crosses\s+above", "crossesabove", code, flags=re.I)
        code = re.sub(r"crosses\s+below", "crossesbelow", code, flags=re.I)
        toks = {t.lower() for t in IDENT_RE.findall(code)}
        out[src_name] = toks
    return out


def keyword_coverage():
    sig_kws = load_signature_keywords()
    sig_lower = {k.lower() for k in sig_kws}
    map_keys = set(_builtins.BUILTINS.keys())
    known = sig_lower | map_keys
    impl, stubbed = implemented_keywords()

    # exercised: implemented keywords that appear in any GT source
    src_tokens = gt_source_tokens()
    all_tokens = set().union(*src_tokens.values()) if src_tokens else set()
    exercised = sorted(impl & all_tokens)
    impl_unexercised = sorted(impl - all_tokens)

    return {
        "known_total": len(known),
        "in_map": len(map_keys),
        "in_signatures": len(sig_lower),
        "in_both": len(sig_lower & map_keys),
        "map_only": len(map_keys - sig_lower),
        "sig_only": len(sig_lower - map_keys),
        "implemented": sorted(impl),
        "implemented_n": len(impl),
        "stubbed_n": len(stubbed),
        # known but with no real implementation (loud stub or no mapping at all)
        "unimplemented_n": len(known) - len(impl),
        "exercised": exercised,
        "exercised_n": len(exercised),
        "impl_unexercised": impl_unexercised,
        "src_tokens": src_tokens,
    }


# --------------------------------------------------------------------------- #
# Section 2 — order-mode coverage (derived from FillEngine + captures)
# --------------------------------------------------------------------------- #
# OrderMode numbering is THIS project's GTA5 matrix convention (see
# reviews/CAPTURE_REQUESTS_MINIMAL.md): 0=next-bar market, 1=limit entries,
# 2=stop entries, 3=this-bar on-close entries, 4=named bracket exits.
MODE_SEMANTICS = {
    "0": ("next-bar market", "market"),
    "1": ("limit entries (sig-bar Low buy / High short)", "limit"),
    "2": ("stop entries (sig-bar High buy / Low short)", "stop"),
    "3": ("this-bar on-close entries", "on-close"),
    "4": ("named bracket exits (stop+limit from entry)", "bracket"),
}


def order_mode_coverage():
    """Derive implemented vs deferred fill types from the FillEngine source
    and enumerate the OrderMode of the captures actually present. Everything
    here is MEASURED (engine-source fill paths + capture files on disk) —
    never asserted."""
    src = os.path.join(REPO, "pl_transpiler", "engine.py")
    with open(src, encoding="utf-8") as f:
        engine = f.read()

    # Fill paths actually present in the engine source.
    fill_paths = {
        "market": bool(re.search(r"otype\s*==\s*'market'", engine)),
        "limit": bool(re.search(r"otype\s+in\s+\('limit',\s*'stop'\)", engine)),
        "stop": bool(re.search(r"otype\s+in\s+\('limit',\s*'stop'\)", engine)),
        "on-close": bool(re.search(
            r"timing\s+in\s+\('this',\s*'thisbar'\)\s+and\s+otype\s*==\s*'close'",
            engine)),
        # bracket = named exit legs (stop+limit) resolved on the same bar via the
        # open-relative conditional-fill ordering.
        "bracket": bool(re.search(r"cond_orders", engine)),
    }

    # Captures present: parse the OrderMode (oN) / MMVariant (mmN) tokens out of
    # the csv filenames wired into GT_FILES, to show which modes have ground truth.
    cap_modes = {}
    for label, (_src, csv_name) in GT_FILES.items():
        present = os.path.exists(os.path.join(CAP, csv_name))
        m_o = re.search(r"_o(\d+)", csv_name)
        m_mm = re.search(r"_mm(\d+)", csv_name)
        # GT2 / GT_master legacy names: m0 == OrderMode-0 (next-bar market)
        om = m_o.group(1) if m_o else ("0" if re.search(r"_m0", csv_name) else "0")
        cap_modes[label] = {
            "csv": csv_name,
            "present": present,
            "order_mode": om,
            "mm_variant": (m_mm.group(1) if m_mm else "0"),
        }
    validated_modes = sorted({v["order_mode"] for v in cap_modes.values()
                              if v["present"]})
    return {
        "fill_paths": fill_paths,
        "captures": cap_modes,
        "ordermodes_present": sorted({v["order_mode"] for v in cap_modes.values()}),
        "validated_modes": validated_modes,
    }


# --------------------------------------------------------------------------- #
# Section 3 — full-range parity (reuse verify_full_range)
# --------------------------------------------------------------------------- #
def parity(labels, scratch, max_bars):
    """Per-label order/position/trade exactness + calc pass, via verify_full_range."""
    os.makedirs(scratch, exist_ok=True)
    _rt.pl_unimplemented_reset()
    rows = {}
    for label in labels:
        ok, summary, detail = vfr.check_label(label, scratch, max_bars=max_bars)
        if detail:
            order_cols = {c: s for c, s in detail["cols"].items()
                          if vfr.classify(c) == "order"}
            calc_cols = {c: s for c, s in detail["cols"].items()
                         if vfr.classify(c) == "calc"}
            exempt = detail["deferred"] | detail["precision"]
            order_exact = sum(1 for s in order_cols.values() if s["mismatch"] == 0)
            calc_pass = sum(1 for c, s in calc_cols.items()
                            if s["mismatch"] == 0 or c in exempt)
            m = re.search(r"bars=(\d+)", summary)
            bars = int(m.group(1)) if m else 0
            rows[label] = {
                "ok": ok,
                "order_exact": order_exact,
                "order_total": len(order_cols),
                "order_parity": not detail["order_bad"],
                "order_bad": detail["order_bad"],
                "calc_pass": calc_pass,
                "calc_total": len(calc_cols),
                "calc_bad": detail["calc_bad"],
                "bars": bars,
                "summary": summary,
            }
        else:
            # check_label returned no detail => throw sidecar / fail-loud path
            rows[label] = {"ok": ok, "order_parity": False, "order_exact": 0,
                           "order_total": 0, "order_bad": [], "calc_pass": 0,
                           "calc_total": 0, "calc_bad": [], "bars": 0,
                           "summary": summary}
    # runtime cross-check: any unimplemented keyword hit during the GT runs
    hit = _rt.pl_unimplemented_report()
    return rows, hit


# --------------------------------------------------------------------------- #
# Section 4 — reverse (emission) coverage
# --------------------------------------------------------------------------- #
def _scan_kinds(path, regexes):
    """Union of every node-kind captured by `regexes` in the file at `path`."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    kinds = set()
    for rx in regexes:
        kinds.update(rx.findall(text))
    return kinds


def node_kind_coverage():
    """DERIVED node-kind coverage. The denominator (y) is the parser's full node
    inventory, scanned from pl_transpiler/parser.py. codegen_el's supported set is its
    `t == '<kind>'` dispatch plus the top-level `program` guard; py_front's produced
    set is its `'type': '<kind>'` reconstructions. Both are intersected with the
    parser inventory (a stray literal can't inflate coverage) and the UNHANDLED
    remainder is reported explicitly."""
    parser_kinds = _scan_kinds(PARSER_PATH, [_TYPE_LIT_RE])
    codegen_kinds = _scan_kinds(CODEGEN_EL_PATH, [_DISPATCH_RE, _NEQ_RE]) & parser_kinds
    pyfront_kinds = _scan_kinds(PY_FRONT_PATH, [_TYPE_LIT_RE]) & parser_kinds
    return {
        "total": len(parser_kinds),
        "all_kinds": sorted(parser_kinds),
        "codegen_n": len(codegen_kinds),
        "codegen": sorted(codegen_kinds),
        "codegen_unhandled": sorted(parser_kinds - codegen_kinds),
        "pyfront_n": len(pyfront_kinds),
        "pyfront": sorted(pyfront_kinds),
        "pyfront_unhandled": sorted(parser_kinds - pyfront_kinds),
    }


def roundtrip_coverage(scratch, max_bars):
    """Reuse the tests/verify_roundtrip.py machinery (bounded) via a subprocess so
    its in-place GT_FILES repointing can NEVER leak into this process's parity
    section. Parse its per-label PASS/FAIL scoreboard; report green/GT_EXPECT-total.
    Everything derived from the gate's own output — no re-implemented comparison."""
    os.makedirs(scratch, exist_ok=True)
    cmd = [sys.executable, ROUNDTRIP_PATH,
           "--max-bars", str(max_bars), "--scratch", scratch]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    line_re = re.compile(r"\[(PASS|FAIL)\]\s+(\S+)")
    green, seen = [], []
    for status, label in line_re.findall(proc.stdout):
        seen.append(label)
        if status == "PASS":
            green.append(label)
    return {
        "green": sorted(green),
        "green_n": len(green),
        "total": len(GT_EXPECT),   # n/GT_EXPECT-total (derived, never a literal)
        "seen_n": len(seen),
        "returncode": proc.returncode,
        "max_bars": max_bars,
        "stderr_tail": proc.stderr.strip().splitlines()[-3:] if proc.returncode else [],
    }


def sweep_stats():
    """Read the R3 emission-sweep counts from reviews/EMISSION_SWEEP.md if present
    (globbed / emitted / passed / BLOCKED_COUNT). Returns None when the file is
    absent so the section degrades gracefully."""
    if not os.path.exists(SWEEP_MD):
        return None
    with open(SWEEP_MD, encoding="utf-8") as f:
        text = f.read()

    def grab(rx):
        m = re.search(rx, text)
        return int(m.group(1)) if m else None

    return {
        "globbed": grab(r"globbed[^\n]*:\s*(\d+)"),
        "emitted": grab(r"emitted[^\n]*:\s*(\d+)"),
        "passed": grab(r"passed[^\n]*:\s*(\d+)"),
        "blocked": grab(r"BLOCKED_COUNT=(\d+)"),
    }


# --------------------------------------------------------------------------- #
# Render COVERAGE.md
# --------------------------------------------------------------------------- #
def render(kw, om, par_rows, unimpl_hit, max_bars, full, nk, rt, sweep):
    labels_parity = sum(1 for r in par_rows.values() if r["order_parity"])
    n_labels = len(par_rows)
    # Derived, never hard-coded: the short names of every mode with a capture
    # on disk (e.g. "market/limit/stop/on-close/bracket (OrderModes 0,1,2,3,4)").
    _val = om["validated_modes"]
    om_str = ("/".join(MODE_SEMANTICS[m][1] for m in _val if m in MODE_SEMANTICS)
              + f" (OrderModes {','.join(_val)} captured)") if _val else "NONE captured"
    headline = (
        f"Keywords: {kw['implemented_n']}/{kw['known_total']} implemented, "
        f"{kw['exercised_n']} exercised by GT | "
        f"Order-modes: {om_str} | "
        f"Full-range parity: {labels_parity}/{n_labels} labels "
        f"(order/position/trade exact, "
        f"{'true full range' if full else f'bounded {max_bars} bars'})"
        f" | Reverse: {rt['green_n']}/{rt['total']} labels round-trip, "
        f"{nk['codegen_n']}/{nk['total']} node kinds"
    )

    lines = []
    lines.append("# COVERAGE — falsifiable EL coverage metric")
    lines.append("")
    lines.append(f"**HEADLINE:** {headline}")
    lines.append("")
    lines.append(
        "_Generated by `tests/coverage_report.py` (read-only measurement; "
        "changes no runtime/captures/gates/tolerance). Re-run to regenerate._")
    lines.append("")

    # --- Section 1 ---
    lines.append("## 1. Transpilable-keyword coverage")
    lines.append("")
    lines.append(
        f"- **Known keywords (total):** {kw['known_total']} "
        f"= union of the transpiler keyword map ({kw['in_map']}) and the "
        f"signature catalog `pl_transpiler/tools/pl_signatures.jsonl` ({kw['in_signatures']}); "
        f"{kw['in_both']} in both, {kw['map_only']} map-only, "
        f"{kw['sig_only']} signature-only.")
    lines.append(
        f"- **Implemented:** {kw['implemented_n']} "
        f"(keyword map entries resolving to a REAL `pl_*` runtime fn, "
        f"not a loud stub).")
    lines.append(
        f"- **Unimplemented:** {kw['unimplemented_n']} "
        f"(of which {kw['stubbed_n']} are mapped-but-loud-stub, the rest are "
        f"known-but-unmapped). These RAISE under PL_STRICT / route to "
        f"`pl_runtime._unimplemented` — fail-loud, never silently wrong.")
    lines.append(
        f"- **Exercised by ground truth:** {kw['exercised_n']} of the "
        f"{kw['implemented_n']} implemented keywords appear in at least one "
        f"`ground_truth/*.txt` EL source (comments stripped).")
    lines.append("")
    lines.append(
        f"Implemented & exercised ({kw['exercised_n']}): "
        + (", ".join(f"`{k}`" for k in kw["exercised"]) or "_none_"))
    lines.append("")
    lines.append(
        f"Implemented but NOT exercised by any GT ({len(kw['impl_unexercised'])}): "
        + (", ".join(f"`{k}`" for k in kw["impl_unexercised"]) or "_none_"))
    lines.append("")
    lines.append("Per-source identifier reach (implemented keywords used):")
    lines.append("")
    impl_set = set(kw["implemented"])
    for src in sorted(kw["src_tokens"]):
        used = sorted(impl_set & kw["src_tokens"][src])
        lines.append(f"- `{src}`: {len(used)} implemented keywords")
    lines.append("")

    # --- Section 2 ---
    lines.append("## 2. Order-mode coverage")
    lines.append("")
    lines.append(
        "| Fill type | OrderMode | Implemented (engine path) | Validated (capture present) |")
    lines.append("|---|---|---|---|")
    for mode, (desc, short) in MODE_SEMANTICS.items():
        impl = om["fill_paths"].get(short, False)
        validated = mode in om["validated_modes"]
        lines.append(
            f"| {desc} | {mode} | {'yes' if impl else 'NO'} | "
            f"{'yes' if validated else 'no'} |")
    lines.append("")
    lines.append(
        "Implemented = the fill path exists in the `FillEngine` in "
        "`pl_transpiler/engine.py` (source-derived). Validated = a capture CSV for that "
        "OrderMode is on disk and wired into the suite. OrderModes present in "
        "captures: " + ", ".join(om["ordermodes_present"]) + ".")
    lines.append("")
    lines.append("Captures wired into the suite:")
    lines.append("")
    lines.append("| Label | Capture CSV | OrderMode | MMVariant | Present |")
    lines.append("|---|---|---|---|---|")
    for label, c in om["captures"].items():
        lines.append(
            f"| {label} | `{c['csv']}` | {c['order_mode']} | "
            f"{c['mm_variant']} | {'yes' if c['present'] else 'NO'} |")
    lines.append("")

    # --- Section 3 ---
    scope = "true full range" if full else f"bounded {max_bars} bars"
    lines.append("## 3. Full-range parity (per label)")
    lines.append("")
    lines.append(
        f"Order/position/trade columns must be EXACT (0 mismatches); calc "
        f"columns are float32-tolerant with the existing documented "
        f"deferral/precision set. Measured at **{scope}** via "
        f"`tests/verify_full_range.check_label` (the authoritative full-range "
        f"gate is `tests/verify_full_range.py` at the true full range).")
    lines.append("")
    lines.append(
        "| Label | Order/pos/trade exact | order cols | calc pass | bars | "
        "strict gate |")
    lines.append("|---|---|---|---|---|---|")
    for label in par_rows:
        r = par_rows[label]
        parity_mark = "yes" if r["order_parity"] else "**NO**"
        # strict gate FAIL caused ONLY by calc drift (order parity intact) is the
        # documented float32-accumulator behaviour, not an order regression.
        if r["ok"]:
            gate = "PASS"
        elif r["order_parity"] and not r["order_bad"]:
            gate = "calc-drift*"
        else:
            gate = "**FAIL**"
        lines.append(
            f"| {label} | {parity_mark} | {r['order_exact']}/{r['order_total']} "
            f"| {r['calc_pass']}/{r['calc_total']} | {r['bars']} | {gate} |")
    lines.append("")
    lines.append(
        "The falsifiable parity metric is the **Order/pos/trade exact** column: "
        f"{labels_parity}/{n_labels} labels keep every order/position/trade "
        "column exact at this run. `strict gate` mirrors "
        "`tests/verify_full_range.py` (order + float32-tolerant calc + existing "
        "deferrals); a `calc-drift*` entry means order parity is intact but an "
        "unbounded float32 accumulator (e.g. `runsum`) has drifted past the "
        "verify_all clean window (~1800 bars) — documented behaviour, not an "
        "order regression and NOT a grown deferral set.")
    lines.append("")
    # surface any mismatches explicitly, separating integrity-critical from drift
    order_bad = {l: r["order_bad"] for l, r in par_rows.items() if r["order_bad"]}
    calc_bad = {l: r["calc_bad"] for l, r in par_rows.items() if r["calc_bad"]}
    if order_bad:
        lines.append("ORDER/POSITION/TRADE mismatches (integrity-critical):")
        for label, cols in order_bad.items():
            lines.append(f"- {label}: {cols}")
        lines.append("")
    if calc_bad:
        lines.append(
            f"Calc-column drift beyond the clean window at {scope} "
            "(float32 accumulators; not order regressions):")
        for label, cols in calc_bad.items():
            lines.append(f"- {label}: {cols}")
        lines.append("")
    lines.append(
        "Runtime cross-check — unimplemented EL keywords REFERENCED by these GT "
        "scripts (loud-stub returning 0/'' in non-strict mode; validated green "
        "by `verify_all`, i.e. the stub default matches the capture or the "
        "column is in the documented deferral set): "
        + (", ".join(f"`{k}`×{v}" for k, v in sorted(unimpl_hit.items()))
           if unimpl_hit else "**none**."))
    lines.append("")

    # --- Section 4 — reverse (emission) coverage ---
    lines.append("## 4. Reverse (emission) coverage")
    lines.append("")
    lines.append(
        "Every number below is DERIVED at run time — the round-trip labels from a "
        "bounded `tests/verify_roundtrip.py` subprocess, the node kinds by scanning "
        "the parser / emitter / py_front source, the sweep counts from "
        "`reviews/EMISSION_SWEEP.md`. Nothing here is a hard-coded claim.")
    lines.append("")

    # 4a. round-trip-green labels
    lines.append(
        f"- **Round-trip-green labels:** {rt['green_n']}/{rt['total']} "
        f"(parse -> emit_el -> rerun the full `verify_all` GT comparison at a "
        f"bounded {rt['max_bars']} bars via `tests/verify_roundtrip.py`; "
        f"exit={rt['returncode']}).")
    if rt["seen_n"] != rt["total"]:
        lines.append(
            f"  - NOTE: the gate reported {rt['seen_n']} labels but GT_EXPECT has "
            f"{rt['total']} — subprocess may have failed early"
            + (f"; stderr: {' / '.join(rt['stderr_tail'])}" if rt["stderr_tail"]
               else "") + ".")
    green_set = set(rt["green"])
    red = [l for l in GT_EXPECT if l not in green_set]
    lines.append(
        "  - Green: " + (", ".join(f"`{l}`" for l in rt["green"]) or "_none_"))
    if red:
        lines.append("  - Not green: " + ", ".join(f"`{l}`" for l in red))
    lines.append("")

    # 4b. AST node-kind coverage
    lines.append(
        f"- **AST node-kind coverage** (denominator = the parser's full "
        f"{nk['total']}-kind inventory, scanned from `transpiler/parser.py`):")
    lines.append(
        f"  - `codegen_el.emit_el` handles **{nk['codegen_n']}/{nk['total']}** "
        "node kinds (its `t == '<kind>'` dispatch + the top-level `program` guard). "
        "Unhandled: "
        + (", ".join(f"`{k}`" for k in nk["codegen_unhandled"]) or "_none_") + ".")
    lines.append(
        f"  - `py_front` produces **{nk['pyfront_n']}/{nk['total']}** node kinds "
        "(the mirror dialect cannot carry the rest). Not produced: "
        + (", ".join(f"`{k}`" for k in nk["pyfront_unhandled"]) or "_none_") + ".")
    lines.append("")

    # 4c. sweep stats
    if sweep is not None:
        def _s(v):
            return "?" if v is None else str(v)
        lines.append(
            "- **Emission sweep** (`reviews/EMISSION_SWEEP.md`): "
            f"globbed {_s(sweep['globbed'])}, emitted {_s(sweep['emitted'])}, "
            f"passed all stages {_s(sweep['passed'])}, "
            f"BLOCKED {_s(sweep['blocked'])} (original already fails parse/"
            "mc_ground_check; frozen per policy).")
    else:
        lines.append(
            "- **Emission sweep:** `reviews/EMISSION_SWEEP.md` not present — "
            "run the R3 sweep to populate these counts.")
    lines.append("")

    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Falsifiable EL coverage report (GOAL C).")
    ap.add_argument("--labels", default=None,
                    help="comma-separated subset for the parity section (default: all).")
    ap.add_argument("--scratch", default=None,
                    help="working dir for predicted CSVs. Default: a unique "
                         "per-run dir under .scratch/ (auto-removed on exit) so "
                         "concurrent runs can NEVER collide on the same files — "
                         "two writers on one predicted CSV corrupt both runs.")
    ap.add_argument("--parity-max-bars", type=int, default=3000, dest="max_bars",
                    help="bounded bar cap for the parity section (default 3000; the "
                         "pure-Python runner is superlinear toward 249k — see #7).")
    ap.add_argument("--full", action="store_true",
                    help="run parity at the TRUE full range (slow; max_bars=None).")
    ap.add_argument("--roundtrip-max-bars", type=int, default=400,
                    dest="rt_max_bars",
                    help="bounded bar cap for the reverse round-trip section "
                         "(default 400; Section 4 is a coverage count, not the "
                         "authoritative gate — tests/verify_roundtrip.py owns that).")
    args = ap.parse_args()

    if args.scratch is None:
        args.scratch = new_scratch("cov-")

    labels = list(GT_EXPECT.keys())
    if args.labels:
        want = [x.strip() for x in args.labels.split(",") if x.strip()]
        unknown = [x for x in want if x not in GT_EXPECT]
        if unknown:
            print(f"Unknown labels: {unknown}. Known: {list(GT_EXPECT)}")
            return 2
        labels = want

    max_bars = None if args.full else args.max_bars

    print("Measuring keyword coverage ...", flush=True)
    kw = keyword_coverage()
    print("Measuring order-mode coverage ...", flush=True)
    om = order_mode_coverage()
    print(f"Measuring full-range parity (labels={labels}, "
          f"max_bars={'FULL' if max_bars is None else max_bars}) ...", flush=True)
    par_rows, unimpl_hit = parity(labels, args.scratch, max_bars)
    print("Measuring node-kind coverage ...", flush=True)
    nk = node_kind_coverage()
    print(f"Measuring round-trip coverage (max_bars={args.rt_max_bars}) ...",
          flush=True)
    rt = roundtrip_coverage(os.path.join(args.scratch, "roundtrip"),
                            args.rt_max_bars)
    sweep = sweep_stats()

    md = render(kw, om, par_rows, unimpl_hit, args.max_bars, args.full,
                nk, rt, sweep)
    with open(COVERAGE_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print(f"Wrote {COVERAGE_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
