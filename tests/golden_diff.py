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
golden_diff.py - validate the pl-transpiler against MultiCharts ground-truth captures.

The captures (ground_truth/captures/*.csv, see MANIFEST.md) are the authoritative
per-bar output produced by running the GT scripts in MultiCharts. This tool has two
independent capabilities:

  1. diff   - float32-tolerant, per-column, bar-aligned diff of a PREDICTED per-bar CSV
              against a capture. This is the core validation and is usable the moment any
              runner can emit a predicted CSV. Self-tested via `selftest`.

  2. probe  - run each GT .txt source through the transpiler and report how far it gets.

NOTE on the full transpile->run->diff path (not yet reachable):
  The current transpiler (a) cannot parse `Inputs:`/`Vars:`/`Arrays:` declaration blocks,
  and (b) its generated `strategy(open,high,low,close,volume,date,time,**kwargs)` returns
  only {orders, plots, alerts, risk} - it does NOT expose the per-bar intermediate
  variables (avg, rsi, counter, ...) that the captures record. Closing those two gaps -
  declaration parsing, and a codegen "trace mode" that emits each variable per bar - is
  what unlocks running a GT script to a predicted CSV. Until then, `probe` reports the
  gap and `diff` validates any externally-produced prediction.

Usage:
  python tests/golden_diff.py selftest
  python tests/golden_diff.py probe
  python tests/golden_diff.py diff <capture.csv> <predicted.csv> [--key bn] [--tol 1e-6]
"""
import csv, struct, sys, os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(REPO, "ground_truth", "captures")
GT = os.path.join(REPO, "ground_truth")
# `from tests import gt_manifest` needs the repo root on sys.path; golden_diff is
# imported standalone (only `tests/` on path) by the item-1 single-source check, so
# add REPO here rather than relying on a caller having done it.
sys.path.insert(0, REPO)
# Map of GT label -> (source_file, capture_file). Single-sourced in
# tests/gt_manifest.py; kept as an INDEPENDENT dict object (a copy) — the reverse
# gates monkeypatch run_gt.GT_FILES and golden_diff.GT_FILES separately and their
# save/restore assumes two distinct objects (aliasing one shared dict would change
# patch semantics).
from tests import gt_manifest  # noqa: E402
GT_FILES = dict(gt_manifest.GT_FILES)


def f32(x):
    """Coerce to IEEE-754 single precision (EasyLanguage stores all numerics as float32)."""
    return struct.unpack("f", struct.pack("f", x))[0]


def _num(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        rows = [row for row in r if row]
    return header, rows


def diff_csvs(capture_path, predicted_path, key="bn", rtol=1e-6, atol=1e-6, max_examples=5):
    """Per-column, bar-aligned, float32-tolerant diff. Returns a report dict."""
    ch, crows = read_csv(capture_path)
    ph, prows = read_csv(predicted_path)
    if key not in ch or key not in ph:
        return {"error": f"key column '{key}' missing (capture={key in ch}, predicted={key in ph})"}
    ck, pk = ch.index(key), ph.index(key)
    cidx = {row[ck]: row for row in crows}
    common_cols = [c for c in ph if c in ch and c != key]
    cols_only_pred = [c for c in ph if c not in ch]
    cols_only_cap = [c for c in ch if c not in ph]
    report = {"common_cols": common_cols, "cols_only_predicted": cols_only_pred,
              "cols_only_capture": cols_only_cap, "columns": {}, "bars_compared": 0,
              "bars_unmatched_key": 0}
    matched_keys = 0
    col_stat = {c: {"match": 0, "mismatch": 0, "examples": []} for c in common_cols}
    for prow in prows:
        crow = cidx.get(prow[pk])
        if crow is None:
            report["bars_unmatched_key"] += 1
            continue
        matched_keys += 1
        for c in common_cols:
            cv, pv = crow[ch.index(c)], prow[ph.index(c)]
            a, b = _num(cv), _num(pv)
            if a is None or b is None:
                ok = (cv.strip() == pv.strip())  # string compare
            else:
                a, b = f32(a), f32(b)
                ok = abs(a - b) <= (atol + rtol * max(abs(a), abs(b)))
            st = col_stat[c]
            if ok:
                st["match"] += 1
            else:
                st["mismatch"] += 1
                if len(st["examples"]) < max_examples:
                    st["examples"].append({"key": prow[pk], "capture": cv, "predicted": pv})
    report["bars_compared"] = matched_keys
    report["columns"] = col_stat
    return report


def probe_transpiler(gt_label):
    """Transpile a GT .txt source; report stage reached and first error."""
    sys.path.insert(0, REPO)
    from pl_transpiler import transpile  # imported here so `diff`/`selftest` need no transpiler
    src_name, _ = GT_FILES[gt_label]
    src = open(os.path.join(GT, src_name), encoding="utf-8").read()
    try:
        out = transpile(src)
        return {"label": gt_label, "stage": "transpiled", "ok": True, "py_lines": out.count("\n") + 1}
    except Exception as e:
        return {"label": gt_label, "stage": "transpile", "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def _print_diff_report(r):
    if "error" in r:
        print("  ERROR:", r["error"]); return
    print(f"  bars compared: {r['bars_compared']}  (unmatched key rows: {r['bars_unmatched_key']})")
    if r["cols_only_capture"]:
        print(f"  columns in capture but NOT predicted: {r['cols_only_capture']}")
    if r["cols_only_predicted"]:
        print(f"  columns in predicted but NOT capture: {r['cols_only_predicted']}")
    nperfect = sum(1 for c, s in r["columns"].items() if s["mismatch"] == 0)
    print(f"  columns matched perfectly: {nperfect}/{len(r['columns'])}")
    for c, s in r["columns"].items():
        if s["mismatch"]:
            ex = s["examples"][0] if s["examples"] else {}
            print(f"    MISMATCH {c}: {s['mismatch']} bars  e.g. bar {ex.get('key')}: "
                  f"capture={ex.get('capture')} predicted={ex.get('predicted')}")


def main(argv):
    if not argv or argv[0] not in ("selftest", "probe", "diff", "run"):
        print(__doc__); return 2
    cmd = argv[0]
    if cmd == "selftest":
        cap = os.path.join(CAP, GT_FILES["GT_master"][1])
        if not os.path.exists(cap):
            # Captures-less clone: SKIP (one-line docs pointer) and return success
            # so the suite stays green; when present, run as before.
            from tests import capture_gate
            print(capture_gate.skip_message("golden_diff selftest")); return 0
        print(f"selftest: diff {os.path.basename(cap)} against itself (expect perfect match)")
        _print_diff_report(diff_csvs(cap, cap))
        return 0
    if cmd == "probe":
        print("Transpiler probe (does each GT .txt transpile?):")
        for label in GT_FILES:
            r = probe_transpiler(label)
            status = "OK -> %d py lines" % r["py_lines"] if r["ok"] else "FAIL -> " + r["error"]
            print(f"  {label:10} {status}")
        return 0
    if cmd == "diff":
        if len(argv) < 3:
            print("usage: diff <capture.csv> <predicted.csv> [--key bn] [--tol 1e-6]"); return 2
        key, tol = "bn", 1e-6
        if "--key" in argv: key = argv[argv.index("--key") + 1]
        if "--tol" in argv: tol = float(argv[argv.index("--tol") + 1])
        print(f"diff: capture={argv[1]} predicted={argv[2]} key={key} tol={tol}")
        _print_diff_report(diff_csvs(argv[1], argv[2], key=key, rtol=tol, atol=tol))
        return 0
    if cmd == "run":
        # Delegate to run_gt.py runner
        sys.path.insert(0, REPO)
        from tests import run_gt as runner
        gt_label = argv[1] if len(argv) > 1 else "GT1"
        max_bars = None
        output = None
        warmup = 0
        i = 2
        while i < len(argv):
            if argv[i] == "--max-bars":
                i += 1; max_bars = int(argv[i])
            elif argv[i] == "--output":
                i += 1; output = argv[i]
            elif argv[i] == "--warmup":
                i += 1; warmup = int(argv[i])
            i += 1
        return runner.run_gt(gt_label, max_bars=max_bars, output_path=output, warmup=warmup)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
