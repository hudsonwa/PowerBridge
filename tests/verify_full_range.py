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

"""verify_full_range.py — FULL-CAPTURE-RANGE partitioned validation  (H3 / GOAL #3).

`verify_all.py` is the single definition of DONE and validates each GT over a
verified-clean 1500-bar window (capture bars 301..1800) where every column matches
at its full count. This script does NOT replace or weaken that gate — it is an
ADDITIONAL, stricter check that runs every GT across the **full capture range**
(max_bars=None) and partitions the compared columns:

  * ORDER / POSITION / TRADE columns  (ORDER_POSITION_TRADE allow-list below):
    discrete, fill-engine-deterministic trade mechanics — order tags, market
    position, contract counts, entry/exit prices & bars, realized P&L, trade
    counts, and the GTA5/GTA6 trade-ledger equivalents. These MUST match the
    capture EXACTLY (0 mismatches) across the FULL range. The unbounded float32
    step-accumulators (e.g. RunSum) that drift past ~1800 bars are NOT in this
    set, so order/position/trade columns are held to an exact bar.

  * CALC / ACCUMULATOR columns  (everything else): indicators, math, datetime,
    averages, percentages, introspection, and the float32 accumulators. These are
    compared with the EXISTING float32-tolerant comparator
    (tests/golden_diff.diff_csvs, rtol=atol=1e-6) and the EXISTING documented
    deferral / precision set (reviews/DEFERRED_INDICATORS.md, surfaced via
    verify_all.GT_EXPECT). The tolerance is reused verbatim and is NOT loosened;
    the deferral set is reused verbatim and does NOT grow here.

Also reuses the H1 throw sidecar: at full range each label must report
throw_count == 0, else it FAILS (a strategy that throws on bars silently leaves
trace={} and predicts 0 — the masked GTA6 cascade).

A label PASSES iff: throw_count == 0 AND every order/position/trade column has 0
mismatches at full range AND every calc column either matches under the float32
comparator or is an existing documented deferral/precision exception.

Exit non-zero on any order/position/trade mismatch, any newly-undeferred calc
mismatch, a missing throw sidecar, or a nonzero throw count.

This run is intentionally slow (full capture range, pure-Python per-bar runner);
progress is printed per label.

Usage:
  python3 tests/verify_full_range.py                 # FULL set — the real criterion
  python3 tests/verify_full_range.py --labels GT2    # subset for iteration
"""
import os
import sys
import time
import argparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests import run_gt
from tests import capture_gate
from tests.repoint import new_scratch
from tests.golden_diff import diff_csvs, GT_FILES, CAP
# Reuse the canonical deferral/precision exemption sets and warmup so this script
# CANNOT silently grow the deferral list or change the tolerance.
from tests.verify_all import GT_EXPECT, WARMUP, _silence

# ---------------------------------------------------------------------------
# Explicit allow-list of ORDER / POSITION / TRADE column names. Membership is by
# column name and applies across every GT (e.g. `marketpos` is a position column
# wherever it appears). Anything NOT in this set is treated as a CALC column and
# is compared under the existing float32 tolerance + deferral set.
#
# These are discrete, fill-engine-deterministic quantities (tags, integer counts,
# bar-derived prices, realized P&L summed over a bounded number of trades) — they
# carry no unbounded float32 step-accumulation, so they must be EXACT at full
# range. Float averages / percentages / symbol-introspection are deliberately
# left OUT (they are calc-style derived stats, not trade mechanics).
# ---------------------------------------------------------------------------
ORDER_POSITION_TRADE = {
    # --- core, named explicitly in the H3 unit (GT2 / GT_master) ---
    "ordertag", "marketpos", "curcontracts", "entryprice",
    "barssinceentry", "openposprofit", "netprofit", "totaltrades",

    # --- GTA5 multi-data ledger equivalents ---
    "avgentryprice", "exitprice", "barssinceexit",
    "positionprofit", "grossprofit", "grossloss",
    "maxcontractsheld", "maxiddrawdown",

    # --- GTA6 file-2 trade-ledger equivalents ---
    # trade counts by outcome
    "nwin", "nlos", "neven",
    # largest realized win / loss
    "lrgwin", "lrglos",
    # contract / position profit & contract-count extremes
    "maxcwin", "maxclos", "maxctrprofit", "maxposprofit", "maxposloss",
    "maxctr", "maxshr", "maxshrheld", "maxentr", "maxposago",
    # total bars in winning / losing / even trades (integer bar counts)
    "totbwin", "totblos", "totbeven",
    # OpenEntry / open-trade ledger (discrete + realized P&L)
    "oecount", "oecontracts", "oeprice", "oeprofit", "oemaxprofit", "oeminprofit",
    "oedate", "oetime",
    # PositionTrades ledger (discrete + realized P&L)
    "ptcount", "ptprofit", "ptsize", "ptislong", "ptisopen",
    "ptentrypx", "ptexitpx", "ptentrybar", "ptexitbar",
    "ptentrydtm", "ptexitdtm", "ptentrycat", "ptexitcat", "ptentrynm", "ptexitnm",
    # most-recent entry / exit name+date+time
    "ennm", "endate", "entime", "endtm",
    "exnm", "exdate", "extime", "exdtm",
    # current open entries / shares / exec offset
    "curentr", "curshr", "execoff",
}


def classify(col):
    return "order" if col in ORDER_POSITION_TRADE else "calc"


def check_label(label, scratch, max_bars=None):
    """Run one GT at FULL range and partition-check its columns.

    Returns (ok, summary_str, detail_dict)."""
    expect = GT_EXPECT[label]
    deferred = set(expect["deferred"])
    precision = set(expect.get("precision", []))
    exempt = deferred | precision  # calc columns allowed to differ (existing set)

    cap = os.path.join(CAP, GT_FILES[label][1])
    out = os.path.join(scratch, f"fullrange_{label.lower()}.csv")

    t0 = time.time()
    # max_bars=None is the true full range, which the runner now completes after the
    # #7 vectorization (incremental indicators + streaming output removed the OOM /
    # superlinearity). A bounded value is only a faster dev-iteration window.
    _silence(run_gt.run_gt, label, max_bars=max_bars, output_path=out, warmup=WARMUP)
    elapsed = time.time() - t0

    # H1 throw sidecar: must exist and report zero throws at full range.
    throws = run_gt.read_throws(out)
    if throws is None:
        return False, f"throw sidecar missing ({elapsed:.0f}s)", {}
    throw_count = throws["throw_count"]
    total_bars = throws["total_bars"]
    if throw_count > 0:
        tb = (throws.get("first_traceback") or "").strip().splitlines()
        tb_last = tb[-1] if tb else ""
        return (False,
                f"FAIL-LOUD: threw on {throw_count}/{total_bars} bars ({tb_last}) "
                f"({elapsed:.0f}s)", {})

    rep = diff_csvs(cap, out)
    if "error" in rep:
        return False, f"diff error: {rep['error']} ({elapsed:.0f}s)", {}

    cols = rep["columns"]
    order_cols = {c: s for c, s in cols.items() if classify(c) == "order"}
    calc_cols = {c: s for c, s in cols.items() if classify(c) == "calc"}

    # ORDER/POSITION/TRADE: any mismatch is fatal — these must be exact.
    order_bad = [c for c, s in order_cols.items() if s["mismatch"]]
    # CALC: a mismatch is fatal ONLY if it is not an existing documented exception.
    calc_bad = [c for c, s in calc_cols.items() if s["mismatch"] and c not in exempt]
    # Every declared deferral/precision column must actually exist in the diff,
    # otherwise the exemption is stale (silently shrinking the real required set).
    missing_exempt = [c for c in exempt if c not in cols]

    order_exact = sum(1 for s in order_cols.values() if s["mismatch"] == 0)
    calc_pass = sum(1 for c, s in calc_cols.items()
                    if s["mismatch"] == 0 or c in exempt)

    ok = (not order_bad) and (not calc_bad) and (not missing_exempt) \
        and throw_count == 0

    summary = (f"order {order_exact}/{len(order_cols)} exact | "
               f"calc {calc_pass}/{len(calc_cols)} pass "
               f"(deferred {sorted(deferred) if deferred else '-'}"
               f"{', precision ' + str(sorted(precision)) if precision else ''}) | "
               f"throws {throw_count}/{total_bars} | "
               f"bars={rep['bars_compared']} | {elapsed:.0f}s")

    detail = {
        "order_bad": order_bad, "calc_bad": calc_bad,
        "missing_exempt": missing_exempt, "cols": cols,
        "deferred": deferred, "precision": precision,
    }
    return ok, summary, detail


def _example(cols, c):
    ex = cols[c]["examples"]
    e = ex[0] if ex else {}
    return (f"{c}: bar {e.get('key')} cap={e.get('capture')} "
            f"pred={e.get('predicted')} ({cols[c]['mismatch']} bars)")


def main():
    ap = argparse.ArgumentParser(
        description="Full-capture-range partitioned validation (H3 / GOAL #3)")
    ap.add_argument("--labels", default=None,
                    help="comma-separated subset (e.g. GT2,GT_master) for "
                         "iteration. DONE-criterion is the FULL set (omit this).")
    ap.add_argument("--scratch", default=None,
                    help="working dir for predicted CSVs. Default: a unique "
                         "per-run dir under .scratch/ (auto-removed on exit) so "
                         "concurrent runs can NEVER collide on the same files — "
                         "two writers on one predicted CSV corrupt both runs.")
    ap.add_argument("--max-bars", type=int, default=None, dest="max_bars",
                    help="cap bars (default: true full range). The default now runs the "
                         "TRUE full 249k range: #7 vectorization (incremental indicators + "
                         "streaming output) removed the OOM/superlinearity, so the whole "
                         "set completes in ~1h (GTA5/GTA6 ~14min each at 249k bars). A "
                         "bounded value is only a faster dev-iteration window (e.g. 20000 "
                         "covers the bar-8851 breakeven bug + the ~8000-bar scale-"
                         "divergence cliff).")
    args = ap.parse_args()

    # Every label here reruns the per-bar GT comparison against the pinned
    # MultiCharts captures. In a captures-less clone SKIP and return success; when
    # present, run exactly as before.
    if not capture_gate.captures_present():
        print(capture_gate.skip_message("full-range partitioned gate"))
        return 0

    if args.scratch is None:
        args.scratch = new_scratch("vfr-")

    os.makedirs(args.scratch, exist_ok=True)
    labels = list(GT_EXPECT.keys())
    if args.labels:
        want = [x.strip() for x in args.labels.split(",") if x.strip()]
        unknown = [x for x in want if x not in GT_EXPECT]
        if unknown:
            print(f"Unknown labels: {unknown}. Known: {list(GT_EXPECT)}")
            return 2
        labels = want

    print("=" * 78)
    print(f"FULL-RANGE PARTITIONED GATE  (warmup={WARMUP}, max_bars=ALL, "
          f"comparator=golden_diff 1e-6)")
    print(f"  order/position/trade allow-list: {len(ORDER_POSITION_TRADE)} names "
          f"(EXACT) | calc: float32-tolerant + existing deferrals")
    print(f"  labels: {labels}"
          + ("" if not args.labels else "   [SUBSET — full set is the criterion]"))
    print("=" * 78)

    all_ok = True
    details = {}
    for label in labels:
        print(f"\n>> {label}: running full capture range "
              f"({GT_FILES[label][1]}) ...", flush=True)
        ok, summary, detail = check_label(label, args.scratch, args.max_bars)
        details[label] = (ok, detail)
        mark = "PASS" if ok else "FAIL"
        print(f"   [{mark}] {label:10s} {summary}", flush=True)
        if not ok and detail:
            for c in detail["order_bad"]:
                print(f"        ORDER/POS/TRADE MISMATCH  {_example(detail['cols'], c)}")
            for c in detail["calc_bad"]:
                print(f"        UNDEFERRED CALC MISMATCH  {_example(detail['cols'], c)}")
            if detail["missing_exempt"]:
                print(f"        STALE EXEMPTION (col absent): {detail['missing_exempt']}")
        all_ok = all_ok and ok

    print("\n" + "=" * 78)
    print("SCOREBOARD (full capture range):")
    for label in labels:
        ok, _ = details[label]
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    scope = "FULL SET" if not args.labels else f"SUBSET {labels}"
    print(f"\nOVERALL ({scope}): {'ALL GREEN' if all_ok else 'MISMATCH'}")
    if not args.labels:
        print("This is the H3 done-criterion (every GT, full range, partitioned).")
    else:
        print("Subset run -- the DONE-criterion is the FULL set (omit --labels).")
    print("=" * 78)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
