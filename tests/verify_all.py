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

"""
verify_all.py — FULL-SUITE regression gate for the pl-transpiler.

This is the single definition of DONE. It runs the ENTIRE suite and prints a
PASS/FAIL scoreboard, exiting non-zero if ANYTHING regresses:

  * GT capture tests (GT1/GT2/GT3/GT4/GT_master) via the shared per-bar runner
    (tests/run_gt.py) + the float32-tolerant comparator (tests/golden_diff.py),
    at their VERIFIED column counts. Deferred indicators
    (reviews/DEFERRED_INDICATORS.md: GT4 stochk/stochd) are NOT chased — they
    are subtracted from the required count, not from the comparator.
  * REVERSE gates (GOAL R5): the ROUNDTRIP (verify_roundtrip) and TWOWAY
    (verify_twoway) binding gates are folded in as permanent scoreboard sections
    so they always run (proof harnesses rot silently when nothing runs them).
    They are bounded-cost, so they run in --fast too; the full-range emitted
    order check stays with Rfull / the orchestrator, not here.
  * OPTIONAL EXTENSION (discovered dynamically): extra gate sections live in
    tests/verify_ext_private.py and are pulled in via importlib. When that module
    is absent this script prints a notice and continues; when present every
    section runs.

CAPTURE-GATING: the capture-dependent gates (the GT/reverse sections here, and
the standalone verify_* gates) SKIP with a one-line docs/CAPTURES.md pointer when
ground_truth/captures/ lacks its pinned files (a captures-less clone), returning
success so the suite stays green out-of-the-box. The skip triggers ONLY on file
absence; when the captures are present the gates run exactly as before.

RULE (FULL-SUITE GATE): a unit is DONE / a change is acceptable ONLY if this
script is fully green. Never mark DONE on a subset; fixing A by regressing B is
forbidden and is caught here.

Usage:
    python tests/verify_all.py                 # full suite, warmup=300, all bars
    python tests/verify_all.py --max-bars 5000 # faster GT runs (still exact)
    python tests/verify_all.py --gt-only
    python tests/verify_all.py --ext-only      # only the private extension sections
"""
import os
import sys
import argparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import importlib

from tests import run_gt
from tests.golden_diff import diff_csvs, GT_FILES, CAP
from tests.repoint import new_scratch
from tests import capture_gate

WARMUP = 300

# Expected perfectly-matched column counts per GT, at warmup=300.
# `deferred` columns are documented in reviews/DEFERRED_INDICATORS.md and are an
# accepted exception: the REQUIRED count is (total_common_cols - deferred).
# `precision` columns use the correct formula but have float32 intermediate
# precision accumulation against the MultiCharts capture; they are also exempted.
GT_EXPECT = {
    "GT1":       {"required": 46, "deferred": [], "precision": []},
    "GT2":       {"required": 13, "deferred": [], "precision": []},
    "GT3":       {"required": 28, "deferred": [], "precision": []},
    # GT4: 41 common cols; correl/stochk/stochd are all documented deferrals in
    # reviews/DEFERRED_INDICATORS.md (correl is MultiCharts-specific 1/7-quantized;
    # stochk/stochd are smoothing-init nuances). 41 - 3 = 38 required.
    "GT4":       {"required": 38, "deferred": ["correl", "stochk", "stochd"], "precision": []},
    "GT_master": {"required": 28, "deferred": [], "precision": []},
    # GTA5: 89 common cols; 4 deferred (corrmc, ofastd, oslowk, oslowd) and 1
    # precision artifact (ad — float32 accumulation over many bars).
    # Required = 89 - 4 - 1 = 84.
    "GTA5":      {"required": 84, "deferred": ["corrmc", "ofastd", "oslowk", "oslowd"],
                   "precision": ["ad"]},
    # GTA5 OrderMode 1-4: same 89-column battery as o0, same deferrals/precision.
    # Order/position columns are exact via the implemented limit/stop/on-close/
    # named-exit fill engine. Required = 89 - 4 - 1 = 84.
    "GTA5_o1":   {"required": 84, "deferred": ["corrmc", "ofastd", "oslowk", "oslowd"],
                   "precision": ["ad"]},
    "GTA5_o2":   {"required": 84, "deferred": ["corrmc", "ofastd", "oslowk", "oslowd"],
                   "precision": ["ad"]},
    "GTA5_o3":   {"required": 84, "deferred": ["corrmc", "ofastd", "oslowk", "oslowd"],
                   "precision": ["ad"]},
    "GTA5_o4":   {"required": 84, "deferred": ["corrmc", "ofastd", "oslowk", "oslowd"],
                   "precision": ["ad"]},
    # GTA6 single-data calc/stats catch-all, split across two capture files.
    # GTA6_1 (file1, 84 comparable cols): all match except `curts` (CurrentTime_s =
    # machine wall-clock, non-reproducible — reviews/DEFERRED_INDICATORS.md). 84-1=83.
    # GTA6_2 (file2, 73 comparable cols): all match (fill-engine-driven stats/introspection).
    "GTA6_1":    {"required": 83, "deferred": ["curts"], "precision": []},
    "GTA6_2":    {"required": 73, "deferred": [], "precision": []},
}

# GT runs use a post-warmup window of this many bars by default. Beyond this the
# pure float32 step-accumulation of the unbounded `RunSum = RunSum + Close`
# accumulator (a U5 calculation column, NOT an order/position column) drifts by
# a few float32 ULPs against the MultiCharts capture — a precision artifact that
# is identical in the U5/U6 "DONE" baseline and independent of the FillEngine.
# 1500 bars (capture bars 301..1800) is the verified-clean scope where every GT
# matches at its full count. Override with --max-bars (e.g. --max-bars 8100 to
# stress the order/position columns, which match exactly out to full range).
DEFAULT_GT_BARS = 1500

# Fill-engine OUTPUT columns (order / position / trade). These must be EXACT (0
# mismatches) at the FULL capture range — the bounded DEFAULT_GT_BARS window above
# is for the calc columns (runsum drift) and CANNOT catch an order-mode bug past its
# horizon: the 1500-bar gate false-greened the GTA5_o4 bracket, which only diverged
# at bn3358. The order-column full-range gate below closes that hole. Unlike calc
# columns, these never carry a float32 precision artifact — MC's orders/PnL are exact.
ORDER_COLS = {
    "marketpos", "curcontracts", "entryprice", "avgentryprice", "barssinceentry",
    "exitprice", "barssinceexit", "openposprofit", "positionprofit", "netprofit",
    "grossprofit", "grossloss", "totaltrades", "maxcontractsheld", "maxiddrawdown",
}

def _silence(fn, *a, **kw):
    """Run fn with stdout suppressed (the runners are chatty)."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


def run_gt_checks(max_bars, scratch):
    os.makedirs(scratch, exist_ok=True)
    results = []
    for label, expect in GT_EXPECT.items():
        cap = os.path.join(CAP, GT_FILES[label][1])
        out = os.path.join(scratch, f"verify_{label.lower()}.csv")
        _silence(run_gt.run_gt, label, max_bars=max_bars,
                 output_path=out, warmup=WARMUP)
        # FAIL-LOUD gate (#1): a GT label FAILS if its strategy threw on ANY bar.
        # The real suite throws 0; a non-zero count means columns were silently
        # predicted 0 (the masked GTA6 cascade) and must NOT count as green.
        throws = run_gt.read_throws(out)
        if throws is None:
            results.append((label, False, "throw sidecar missing", expect))
            continue
        if throws["throw_count"] > 0:
            tb = (throws.get("first_traceback") or "").strip().splitlines()
            tb_last = tb[-1] if tb else ""
            results.append((label, False,
                            f"FAIL-LOUD: strategy threw on "
                            f"{throws['throw_count']}/{throws['total_bars']} bars "
                            f"({tb_last})", expect))
            continue
        rep = diff_csvs(cap, out)
        if "error" in rep:
            results.append((label, False, f"diff error: {rep['error']}", expect))
            continue
        cols = rep["columns"]
        nperfect = sum(1 for s in cols.values() if s["mismatch"] == 0)
        total = len(cols)
        deferred = set(expect["deferred"])
        precision = set(expect.get("precision", []))
        exempt = deferred | precision
        # A failing column is acceptable ONLY if it is documented-deferred or
        # a known precision artifact.
        bad = [c for c, s in cols.items() if s["mismatch"] and c not in exempt]
        # Sanity: every deferred/precision column must actually exist.
        missing_def = [c for c in exempt if c not in cols]
        required = expect["required"]
        ok = (not bad) and (not missing_def) and (nperfect >= required)
        detail = f"{nperfect}/{total} cols perfect (required {required}"
        if deferred:
            detail += f", deferred {sorted(deferred)}"
        if precision:
            detail += f", precision {sorted(precision)}"
        detail += f", bars={rep['bars_compared']})"
        if bad:
            ex = cols[bad[0]]["examples"]
            exs = ex[0] if ex else {}
            detail += f"  UNDEFERRED MISMATCH: {bad} e.g. {bad[0]} bar " \
                      f"{exs.get('key')}: cap={exs.get('capture')} pred={exs.get('predicted')}"
        if missing_def:
            detail += f"  (deferred cols absent from capture: {missing_def})"
        results.append((label, ok, detail, expect))
    return results


def run_order_fullrange_checks(scratch, max_bars=None):
    """For every ORDER-BEARING label, run the FULL capture range and require the
    fill-engine output columns (ORDER_COLS that the label actually has) to be EXACT
    — 0 mismatches. This is what the bounded run_gt_checks above structurally cannot
    guarantee. Calc columns are deliberately NOT checked here (the bounded gate owns
    them, to avoid the documented runsum float32 drift at full range).

    max_bars=None is true full range (the gate); a value is only for fast self-test.
    """
    os.makedirs(scratch, exist_ok=True)
    results = []
    for label in GT_EXPECT:
        cap = os.path.join(CAP, GT_FILES[label][1])
        with open(cap, encoding="utf-8") as f:
            header = set(f.readline().strip().split(","))
        present = sorted(ORDER_COLS & header)
        if not present:
            continue  # pure indicator (GT1/GT3/GT4): no order columns to gate
        out = os.path.join(scratch, f"orderfr_{label.lower()}.csv")
        _silence(run_gt.run_gt, label, max_bars=max_bars, output_path=out, warmup=WARMUP)
        throws = run_gt.read_throws(out)
        if throws is None:
            results.append((label, False, "throw sidecar missing")); continue
        if throws["throw_count"] > 0:
            results.append((label, False,
                            f"FAIL-LOUD: threw on {throws['throw_count']}/{throws['total_bars']} bars")); continue
        rep = diff_csvs(cap, out)
        if "error" in rep:
            results.append((label, False, f"diff error: {rep['error']}")); continue
        cols = rep["columns"]
        bad = [c for c in present if c in cols and cols[c]["mismatch"]]
        ok = not bad
        detail = f"{len(present)} order cols EXACT over {rep['bars_compared']} bars (full range)"
        if bad:
            ex = cols[bad[0]]["examples"]; exs = ex[0] if ex else {}
            detail = (f"ORDER-COLUMN MISMATCH {bad} e.g. {bad[0]} bar {exs.get('key')}: "
                      f"cap={exs.get('capture')} pred={exs.get('predicted')}")
        results.append((label, ok, detail))
    return results


def load_extension():
    """Dynamically discover the PRIVATE suite extension
    (tests/verify_ext_private.py). Returns the imported module, or None when it is
    absent (a public/preview clone). Never fabricates sections — absence is a clear,
    explicit skip, not a silent pass."""
    try:
        return importlib.import_module("tests.verify_ext_private")
    except ModuleNotFoundError as e:
        # Only treat the extension's OWN absence as "not present"; a missing
        # dependency inside a present extension must still surface loudly.
        if e.name in ("tests.verify_ext_private", "verify_ext_private"):
            return None
        raise


def main():
    ap = argparse.ArgumentParser(description="Full-suite regression gate")
    ap.add_argument("--max-bars", type=int, default=DEFAULT_GT_BARS,
                    help=f"GT bars after warmup (default {DEFAULT_GT_BARS}: the "
                         "verified-clean scope).")
    ap.add_argument("--gt-only", action="store_true")
    ap.add_argument("--ext-only", action="store_true",
                    help="run ONLY the private extension sections (skip the GT / "
                         "reverse core). With the extension absent this exits "
                         "non-zero with a clear message - never a vacuous pass.")
    ap.add_argument("--fast", action="store_true",
                    help="skip the full-range order-column gate (calc-window only). "
                         "Fast for iteration / the loop's watch heartbeat - but NOT a "
                         "DONE gate: it cannot catch order-mode bugs past the bounded window.")
    ap.add_argument("--scratch", default=None,
                    help="working dir for predicted CSVs. Default: a unique "
                         "per-run dir under .scratch/ (auto-removed on exit) so "
                         "concurrent runs can NEVER collide on the same files - "
                         "two writers on one predicted CSV corrupt both runs.")
    args = ap.parse_args()

    if args.scratch is None:
        args.scratch = new_scratch("va-")

    # Capture-gating: the GT + reverse core all depend on the pinned MultiCharts
    # captures. In a captures-less clone they SKIP (with a docs/CAPTURES.md pointer)
    # and the suite stays green; when the captures are present they run as before.
    have_caps = capture_gate.captures_present()

    all_ok = True
    print("=" * 70)
    print(f"FULL-SUITE GATE  (warmup={WARMUP}, "
          f"GT max_bars={'all' if args.max_bars is None else args.max_bars})")
    print("=" * 70)

    if not args.ext_only:
        if not have_caps:
            print("\n" + capture_gate.skip_message("GT + reverse core"))
        else:
            print("\nGT capture tests (run_gt.py -> golden_diff comparator):")
            for label, ok, detail, _ in run_gt_checks(args.max_bars, args.scratch):
                mark = "PASS" if ok else "FAIL"
                print(f"  [{mark}] {label:10s} {detail}")
                all_ok = all_ok and ok

            if not args.fast:
                print("\nFull-range order-column gate (fill-engine outputs, EXACT - slow):")
                for label, ok, detail in run_order_fullrange_checks(args.scratch):
                    mark = "PASS" if ok else "FAIL"
                    print(f"  [{mark}] {label:10s} {detail}")
                    all_ok = all_ok and ok
            else:
                print("\n[--fast] skipped the full-range order-column gate "
                      "(NOT a DONE gate; run without --fast or use verify_full_range).")

            # REVERSE gates (GOAL R5): fold the ROUNDTRIP + TWOWAY binding gates in as
            # permanent scoreboard sections. Bounded-cost, so they run in --fast too;
            # the full-range emitted order check belongs to Rfull/the orchestrator, not
            # here. Imported LAZILY: each of those modules imports helpers back from
            # this one, so a top-level import would be circular. They monkeypatch the
            # SHARED run_gt.GT_FILES / golden_diff.GT_FILES in place (repointing them at
            # the emitted EL), so snapshot BOTH and restore before each section —
            # otherwise TWOWAY would read ROUNDTRIP's emitted file as its "original".
            if not args.gt_only:
                from tests import golden_diff as _gd
                from tests import verify_roundtrip as _rt
                from tests import verify_twoway as _tw
                _snap_rg = dict(run_gt.GT_FILES)
                _snap_gd = dict(_gd.GT_FILES)

                def _restore_gt_files():
                    run_gt.GT_FILES.clear(); run_gt.GT_FILES.update(_snap_rg)
                    _gd.GT_FILES.clear(); _gd.GT_FILES.update(_snap_gd)

                print("\nROUNDTRIP gate (EL -> AST -> EL'', run_gt_checks vs emitted):")
                _restore_gt_files()
                rt_rc = _rt.run_default(args.max_bars, os.path.join(args.scratch, "rt"))
                all_ok = all_ok and (rt_rc == 0)

                print("\nTWOWAY gate (EL -> AST -> Py -> AST -> EL'', run_gt_checks vs emitted):")
                _restore_gt_files()
                tw_rc = _tw.run_default(args.max_bars, os.path.join(args.scratch, "tw"))
                all_ok = all_ok and (tw_rc == 0)
                _restore_gt_files()

    # WINDOWS CONSOLE gate (capture-independent — runs with or without captures).
    # Proves the public tier + encoding linter are green under simulated Windows
    # console conditions (cp1252 redirect, ascii redirect, print-site ASCII lint).
    # It is a PRIVATE gate member, deliberately excluded from the public/preview
    # export (like the private extension below), so a public clone will not have it:
    # discover it defensively and, when absent, print a clear notice and continue
    # (never crash on the missing module, never silently pass).
    if not args.gt_only:
        try:
            _wc = importlib.import_module("tests.verify_windows_console")
        except ImportError as e:
            # Only the gate module's OWN absence is a graceful skip; a real import
            # error inside a present module must still surface.
            if e.name not in ("tests.verify_windows_console", "verify_windows_console"):
                raise
            _wc = None
            print("\nWINDOWS CONSOLE gate: tests/verify_windows_console.py not "
                  "present in this clone - section skipped.")
        if _wc is not None:
            wc_ok = _wc.run()
            all_ok = all_ok and wc_ok

    # PRIVATE EXTENSION: discovered dynamically. It owns the parity + bidirectional
    # + cycles sections that depend on the author's private strategy corpus. Absent
    # in a public/preview clone -> print a clear notice and continue (never a silent
    # pass). --ext-only with the extension absent is an explicit failure.
    if not args.gt_only:
        ext = load_extension()
        if ext is None:
            if args.ext_only:
                print("\n" + "=" * 70)
                print("ERROR: --ext-only requested but the private extension "
                      "(tests/verify_ext_private.py) is not present in this clone.")
                print("=" * 70)
                return 1
            print("\nPRIVATE EXTENSION: tests/verify_ext_private.py not present - "
                  "sections skipped (public/preview clone).")
        else:
            all_ok = ext.run(args.fast, args.max_bars, args.scratch) and all_ok

    print("\n" + "=" * 70)
    print(f"OVERALL: {'ALL GREEN' if all_ok else 'REGRESSION'}")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
