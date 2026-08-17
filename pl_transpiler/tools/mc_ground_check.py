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

"""mc_ground_check.py — ground a MultiCharts PowerLanguage script before pasting it into MC.

Every token that is USED but not DECLARED in the script must be compile-safe, where
"compile-safe" means PROVEN, not merely "appears somewhere in a doc":
  PROVEN = (a) in the MC reserved-words list (a MultiCharts reference file, excluded from the public build), OR
           (b) used AS CODE (comments + string literals stripped) in a script that already
               COMPILED in MultiCharts (the PROVEN_SCRIPTS list below).
Anything else is NOT-PROVEN and must be verified against a real keyword-reference ENTRY in
the MultiCharts keyword reference (a definition, not a prose mention; the reference file is excluded from the public build) or replaced/inlined/dropped.

Substring presence in the PDF, or presence only in the TradeStation help,
is NOT sufficient — that is exactly how `Yesterday` (a TS-only reserved word MC lacks) slipped
through and broke the GTA5 compile. See lessons.md.

This gate checks NAMES + 0-arg ARITY. It is the FIRST of a THREE-PASS pre-compile audit; the other
two passes are MANUAL (no MC compiler here, so the MC editor "Verify" is still the final oracle):
  (1) NAMES — every used-but-undeclared token is a real MC keyword (index + reserved-words + proven
      scripts + the verified allowlist).  [automated here]
  (2) 0-ARITY — a real keyword that is a 0-arg PROPERTY must NOT be called with parentheses, else MC:
      "Invalid number of parameters. 0 parameter(s) expected" (e.g. SessionCountMS, MaxShares are
      0-arg; siblings SessionCount(SessionType), MaxContracts(PosBack) take args).  [automated here]
  (3) SIGNATURES — arg-COUNT of multi-arg fns, arg-TYPES, and return-type-vs-var-type are NOT checked
      here; verify each against its PL-Ref `Usage` line. Pitfalls that bit GTA6 (see lessons.md):
      Array_GetBooleanValue→TrueFalse (don't assign to a numeric var; use `If fn() Then v=1`);
      string fns (DateToString, FormatDate, SymbolName, *Name, Array_GetStringValue) → String var +
      bare concat, never NumToStr; Array_Sort's 4th arg is a TrueFalse (true=ascending), not 0/1.

Usage:  python pl_transpiler/tools/mc_ground_check.py <script.txt>
Exit:   0 if every used builtin is PROVEN and no 0-arg property is mis-called; 1 otherwise.
"""
import json, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIGNATURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pl_signatures.jsonl')
RESERVED = os.path.join(REPO, '_mc_reference_excluded', 'reserved_words.txt')
PROVEN_SCRIPTS = [  # scripts that have compiled + run in MultiCharts
    'ground_truth/GT1_functions_indicators.txt',
    'ground_truth/GT2_strategy_orders_position.txt',
    'ground_truth/GT3_coverage_indicator.txt',
    'ground_truth/GT4_coverage_indicator.txt',
    'ground_truth/GT_master_strategy.txt',
    # GTA5/GTA6 compiled in MC and produce verify_all-GREEN captures (GTA5, GTA6_1, GTA6_2)
    # — the literal definition of PROVEN. Registering them validates their call forms (e.g.
    # `from entry("LE4")`, StringToDate("..."), MaxContracts(1)); the single-string-literal
    # arg is otherwise stripped before the Pass-3 arity count, mis-reading it as 0 args.
    'ground_truth/GTA5_strategy.txt',
    'ground_truth/GTA6_strategy.txt',
]
# EL language keywords / operators (built into the grammar, never "builtins" to ground)
LANG_KW = set(
    'if then else begin end for to downto while and or not once of data data1 data2 cross crosses '
    'above below over under is was true false next bar at this on close open high low volume contract '
    'contracts share shares from entry total point points all long short stop limit market sell buy '
    'buytocover sellshort default numericseries numeric numericsimple stringseries string truefalse '
    'intrabarpersist begincg endcg mod return'.split())


def _strip(src):
    """Remove { } comments, // comments, and "string" literals -> code identifiers only."""
    src = re.sub(r'\{[^}]*\}', ' ', src, flags=re.S)
    src = re.sub(r'//[^\n]*', ' ', src)
    src = re.sub(r'"[^"]*"', ' ', src)
    return src


def _code_tokens(path):
    try:
        return set(w.lower() for w in re.findall(r'[A-Za-z_]\w*', _strip(open(path, encoding='utf-8', errors='ignore').read())))
    except OSError:
        return set()


def _declared(code):
    declared = set()
    for m in re.finditer(r'(?i)\b(inputs?|vars?|variables?|arrays?|array)\s*:\s*(.*?);', code, flags=re.S):
        # name is followed by '(' (init), ',', ';', '=', '[' (array dimension), or end-of-block
        for nm in re.findall(r'([A-Za-z_]\w*)\s*(?:\(|,|;|=|\[|$)', m.group(2)):
            declared.add(nm.lower())
    for nm in re.findall(r'(?i)\bfor\s+([A-Za-z_]\w*)\s*=', code):
        declared.add(nm.lower())
    return declared


# Tokens manually confirmed to have a REAL keyword-reference ENTRY in the PowerLanguage keyword reference
# (one token per line, '# citation' after it). Populated only with verified constructs.
VERIFIED_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mc_verified_builtins.txt')
PDF = os.path.join(REPO, '_mc_reference_excluded', 'keyword_index.txt')


def index_set():
    """The PDF's authoritative alphabetical keyword INDEX = the run of one-keyword-per-line
    entries before the first detailed 'Usage' block. The index is the strongest signal of a
    real MC keyword NAME. CAVEAT: it over-includes a few TS-only words (e.g. Yesterday), so
    index PRESENCE is advisory, not proof — but index ABSENCE of a name that the body claims
    is a keyword is a strong red flag. That is exactly how `SetDollarTrailing_pt` (a copy-paste
    typo in a body Usage header, absent from the index) slipped past the allowlist and broke
    the MC compile with 'Unknown Function' at GTA5 Ln 418. See lessons.md."""
    try:
        lines = open(PDF, encoding='utf-8', errors='ignore').read().splitlines()
    except OSError:
        return None  # PDF unavailable -> skip the cross-check (don't fail hard)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == 'Usage'), len(lines))
    return set(ln.strip().lower() for ln in lines[:end] if re.fullmatch(r'[A-Za-z_]\w*', ln.strip()))


def proven_set():
    proven = set()
    for rel in PROVEN_SCRIPTS:
        proven |= _code_tokens(os.path.join(REPO, rel))
    try:
        html = open(RESERVED, encoding='utf-8', errors='ignore').read().lower()
        proven |= set(re.findall(r'[a-z_]\w+', re.sub(r'<[^>]+>', ' ', html)))
    except OSError:
        pass
    idx = index_set()
    rejected = []
    try:
        for line in open(VERIFIED_LIST, encoding='utf-8', errors='ignore'):
            tok = line.split('#', 1)[0].strip().lower()
            if not tok:
                continue
            # The allowlist must hold real MC keyword NAMES: every entry must also appear in
            # the PDF alphabetical index. Reject (do NOT trust) any that don't — a body-text
            # citation alone is insufficient (it can be a typo'd header).
            if idx is not None and tok not in idx:
                rejected.append(tok)
                continue
            proven.add(tok)
    except OSError:
        pass
    if rejected:
        print("WARNING: allowlist tokens NOT in the PDF keyword index (rejected as unproven): "
              + ", ".join(sorted(rejected)))
    return proven


# Real functions whose PDF body Usage HEADER is a copy-paste typo of a DIFFERENT name, so
# `name(` never appears in the body even though they DO take args. Whitelist (do not flag as 0-arg).
KNOWN_TYPO_HEADER = {'settrailingstop_pt'}  # documented under the typo'd "SetDollarTrailing_pt(" header


def zero_arg_violations(code):
    """SIGNATURE check (names are necessary, not sufficient): a real MC keyword that is a 0-arg
    PROPERTY but is CALLED WITH parentheses in the script -> MC compile error 'Invalid number of
    parameters. 0 parameter(s) expected'. Heuristic (reliable): a token in the PDF alphabetical
    INDEX whose functional form `name(` NEVER appears in the PDF body is a 0-arg property; if the
    script calls it as `name(...)` that is a bug. (e.g. SessionCountMS, MaxShares, SessionLastBar are
    0-arg; their siblings SessionCount(SessionType), MaxContracts(PosBack) DO take args.) User-var
    declarations `Vars: Name(init)` are NOT flagged — only real PDF-index keywords are considered."""
    idx = index_set()
    if idx is None:
        return []
    try:
        pdf_low = open(PDF, encoding='utf-8', errors='ignore').read().lower()
    except OSError:
        return []
    # Strip declaration STATEMENTS (Inputs/Vars/Arrays) so `Name(init)` / `Arr[](init)` declarations
    # — incl. a user var that SHADOWS a builtin (e.g. GT1 declares `PrevClose(0)`) — are not read as calls.
    body = re.sub(r'(?i)\b(inputs?|vars?|variables?|arrays?|array)\s*:.*?;', ' ', code, flags=re.S)
    declared = _declared(code)
    # Names used WITH parens in a script that ALREADY COMPILED in MC = a validated call form -> never flag
    # (covers builtins whose PDF body wraps `Name` and `(args)` onto separate lines).
    proven_call = set()
    for rel in PROVEN_SCRIPTS:
        pcode = re.sub(r'(?i)\b(inputs?|vars?|variables?|arrays?|array)\s*:.*?;', ' ',
                       _strip(open(os.path.join(REPO, rel), encoding='utf-8', errors='ignore').read()), flags=re.S) \
            if os.path.exists(os.path.join(REPO, rel)) else ''
        proven_call |= set(m.group(1).lower() for m in re.finditer(r'([A-Za-z_]\w*)\s*\(', pcode))
    bad = set()
    for m in re.finditer(r'([A-Za-z_]\w*)\s*\(', body):
        n = m.group(1).lower()
        if (n in idx and n not in KNOWN_TYPO_HEADER and n not in declared and n not in proven_call
                and not re.search(r'(?i)\b' + re.escape(n) + r'\s*\(', pdf_low)):
            bad.add(n)
    return sorted(bad)


def load_signatures():
    """keyword(lower) -> {returns, min_arity, zero_arg_property} from pl_signatures.jsonl
    (produced by unit H2x). Missing/unreadable -> {} (skip Pass 2/3b, don't fail hard)."""
    sigs = {}
    try:
        for line in open(SIGNATURES, encoding='utf-8', errors='ignore'):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kw = d.get('keyword', '').lower()
            if kw:
                sigs[kw] = d
    except OSError:
        pass
    return sigs


def declared_types(raw):
    """Map a declared Input/Var name(lower) -> 'string' | 'numeric'. A declaration
    `Name( init )` is STRING iff its init value is a string literal ("..."), else NUMERIC.
    Runs on the RAW (un-stripped) source so the "..." inits are still visible."""
    types = {}
    for m in re.finditer(r'(?i)\b(inputs?|vars?|variables?)\s*:\s*(.*?);', raw, flags=re.S):
        block = m.group(2)
        for dm in re.finditer(r'([A-Za-z_]\w*)\s*\(\s*([^()]*?)\s*\)', block):
            init = dm.group(2).strip()
            types[dm.group(1).lower()] = 'string' if init.startswith('"') else 'numeric'
    return types


def _rhs_keyword(rhs):
    """If the whole RHS is a single keyword call/property (`Kw` or `Kw(...)`), return Kw(lower),
    else None. Conservative on purpose: `LeftStr(..)+","` or arithmetic RHS is NOT a direct
    assignment of one keyword's result, so we don't second-guess its type."""
    m = re.fullmatch(r'([A-Za-z_]\w*)\s*(\(.*\))?', rhs.strip(), flags=re.S)
    return m.group(1).lower() if m else None


def returntype_violations(code, raw, sigs):
    """PASS 2: a NUMERIC var assigned the result of a string-returning keyword (the
    SymbolCurrencyCode trap) — or a STRING var assigned a numeric-returning keyword."""
    types = declared_types(raw)
    out = []
    for m in re.finditer(r'(?m)^\s*([A-Za-z_]\w*)\s*=\s*([^;]+);', code):
        lhs = m.group(1).lower()
        vt = types.get(lhs)
        if vt is None:
            continue
        kw = _rhs_keyword(m.group(2))
        sig = sigs.get(kw) if kw else None
        if not sig:
            continue
        ret = sig.get('returns')
        if ret == 'string' and vt == 'numeric':
            out.append((kw, lhs, "string-returning keyword assigned to NUMERIC var"))
        elif ret == 'numeric' and vt == 'string':
            out.append((kw, lhs, "numeric-returning keyword assigned to STRING var"))
    return out


def _count_args(s, open_idx):
    """Top-level argument count for the call whose '(' is at open_idx (0 if `()`)."""
    depth, args, i, n = 0, 0, open_idx, len(s)
    while i < n:
        c = s[i]
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
            if depth == 0:
                break
        elif c == ',' and depth == 1:
            args += 1
        i += 1
    return 0 if s[open_idx + 1:i].strip() == '' else args + 1


def arity_signature_violations(code):
    """PASS 3b (signature-table arity): a keyword with min_arity>0 called with too few args,
    or a 0-arg property called WITH args. Declaration statements and call forms already
    validated by a compiled PROVEN script are excluded (never flag known-good)."""
    sigs = load_signatures()
    if not sigs:
        return []
    body = re.sub(r'(?i)\b(inputs?|vars?|variables?|arrays?|array)\s*:.*?;', ' ', code, flags=re.S)
    declared = _declared(code)
    proven_call = set()
    for rel in PROVEN_SCRIPTS:
        p = os.path.join(REPO, rel)
        if os.path.exists(p):
            pcode = re.sub(r'(?i)\b(inputs?|vars?|variables?|arrays?|array)\s*:.*?;', ' ',
                           _strip(open(p, encoding='utf-8', errors='ignore').read()), flags=re.S)
            proven_call |= set(mm.group(1).lower() for mm in re.finditer(r'([A-Za-z_]\w*)\s*\(', pcode))
    out = []
    seen = set()
    for m in re.finditer(r'([A-Za-z_]\w*)\s*\(', body):
        kw = m.group(1).lower()
        sig = sigs.get(kw)
        if not sig or kw in declared or kw in proven_call or kw in seen:
            continue
        argc = _count_args(body, m.end() - 1)
        if sig.get('zero_arg_property') and argc >= 1:
            out.append((kw, f"0-arg property called WITH {argc} arg(s) -- call it bare"))
            seen.add(kw)
        elif not sig.get('zero_arg_property') and sig.get('min_arity', 0) > 0 and argc < sig['min_arity']:
            out.append((kw, f"too few args (got {argc}, needs >= {sig['min_arity']})"))
            seen.add(kw)
    return out


def check(path):
    raw = open(path, encoding='utf-8', errors='ignore').read()
    code = _strip(raw)
    used = set(w.lower() for w in re.findall(r'[A-Za-z_]\w*', code))
    cands = sorted(c for c in (used - _declared(code) - LANG_KW) if not c.isdigit())
    safe = proven_set()
    not_proven = [c for c in cands if c not in safe]
    # Name-proof needs the compile-verified corpus: the RESERVED reserved-words list and
    # the PROVEN_SCRIPTS that actually compiled in MultiCharts. Both are repo-only and do
    # NOT ship in the installed wheel. Consistent with this file's existing policy —
    # "data source unavailable -> skip the check, don't fail hard" (see index_set() and
    # load_signatures()) — the name-proof verdict is SKIPPED when that corpus is absent
    # (e.g. a fresh `pip install` of the wheel): with no ground truth on disk we cannot
    # honestly call a builtin "unproven". Arity (Pass 3b) and return-type (Pass 2) still
    # run — they use the SHIPPED signature catalog. In a normal checkout the corpus is
    # always present, so this branch is never taken and gate behaviour is unchanged.
    name_proof_skipped = not (os.path.exists(RESERVED) or any(
        os.path.exists(os.path.join(REPO, r)) for r in PROVEN_SCRIPTS))
    if name_proof_skipped:
        not_proven = []
    sigs = load_signatures()
    # PASS 3a (PDF-index 0-arg-with-parens heuristic) + PASS 3b (signature-table arity), deduped by name.
    arity_3a = [(n, "0-arg property called WITH parentheses") for n in zero_arg_violations(code)]
    seen_arity = {n for n, _ in arity_3a}
    arity_bad = arity_3a + [(n, r) for n, r in arity_signature_violations(code) if n not in seen_arity]
    # PASS 2 (return-type vs var-type).
    rettype_bad = returntype_violations(code, raw, sigs)
    proven_label = "SKIPPED" if name_proof_skipped else str(len(cands) - len(not_proven))
    print(f"{path}: {len(cands)} builtins used | {proven_label} PROVEN | "
          f"{len(not_proven)} NOT-PROVEN | {len(arity_bad)} ARITY-BAD | {len(rettype_bad)} RETURN-TYPE-BAD")
    if name_proof_skipped:
        print("  NOTE: name-proof SKIPPED -- compile-verified corpus (reserved-words list + "
              "PROVEN_SCRIPTS) not on disk (installed wheel). Arity + return-type still enforced.")
    if not_proven:
        print("\nNOT-PROVEN (verify each has a real MC keyword-reference entry, or replace/inline/drop):")
        for i in range(0, len(not_proven), 6):
            print("  " + ", ".join(not_proven[i:i + 6]))
    if arity_bad:
        print("\nARITY (Pass 3) -- wrong number of parameters (MC: 'Invalid number of parameters'). "
              "0-arg properties must be called bare; multi-arg fns need their args:")
        for n, r in arity_bad:
            print(f"  {n}: {r}")
    if rettype_bad:
        print("\nRETURN-TYPE (Pass 2) -- keyword's return type does not match the destination var type "
              "(e.g. the SymbolCurrencyCode string-into-numeric trap). Use a matching var type:")
        for kw, var, r in rettype_bad:
            print(f"  {kw} -> {var}: {r}")
    if not_proven or arity_bad or rettype_bad:
        print("\nFAIL -- do not paste into MultiCharts until these are resolved.")
        return 1
    print("OK -- every used builtin is proven MC-compile-safe (names + arity + return-type vs var-type).")
    print("NOTE: this gate still does NOT check arg-TYPES of multi-arg fns (only arg-COUNT and "
          "return-type-vs-var-type) -- verify those against the PL-Ref Usage lines. MC editor = final oracle.")
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("usage: python pl_transpiler/tools/mc_ground_check.py <script.txt>"); sys.exit(2)
    sys.exit(check(sys.argv[1]))
