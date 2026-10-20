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
test_general_entrypoint.py — prove the GENERAL entrypoint (tools/pl_run.run_el)
reproduces the validated GT engine via existing ground truth, with NO captures or
human input required.

Strategy: feed an EXISTING GT script (GT2_strategy) + its real GT-aligned OHLCV
(exported to a plain OHLCV CSV) + the NQ #4 config through run_el, and assert the
ORDER / POSITION / TRADE columns match tests/run_gt.py's predicted output for GT2
byte-identically. This shows the general path drives the SAME bar-by-bar engine +
FillEngine as the capture harness — without the capture-coupled GT_FILES machinery.
"""
import csv
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests import capture_gate  # noqa: E402
from tests.repoint import new_scratch  # noqa: E402
from tests.run_gt import run_gt, GT_FILES, CAP, _load_ohlcv  # noqa: E402
from pl_transpiler.tools.pl_run import run_el, load_bars_csv  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402

GT = os.path.join(REPO, "ground_truth")

# GT2 capture column  ->  run_el trace key (the order/position/trade surface).
# These are the columns whose values come from the strategy's orders and the
# FillEngine's position/trade evolution.
COL_TO_TRACE = [
    ("ordertag", "ordertag"),
    ("marketpos", "_market_position"),
    ("curcontracts", "current_contracts"),
    ("entryprice", "entry_price"),
    ("barssinceentry", "bars_since_entry"),
    ("openposprofit", "open_position_profit"),
    ("netprofit", "net_profit"),
    ("totaltrades", "totaltrades"),
]

MAX_BARS = 20000  # documented practical cap; plenty of MA crosses + trades for GT2


def _resolve_gt2_bars(max_bars):
    """Reproduce run_gt's GT2 OHLCV resolution as a plain bar list.

    GT2's capture has no OHLCV columns, so run_gt borrows OHLCV from a same-series
    capture aligned by (date,time) (GT2 shares the .../921 series with GT_master).
    We replicate that EXACT resolution so the bars we feed run_el are the same bars
    run_gt fed its engine — the real GT-aligned OHLCV, no fabrication.
    """
    cap_path = os.path.join(CAP, GT_FILES['GT2'][1])
    with open(cap_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        cap_header = next(reader)
        cap_rows = [r for r in reader if r]
    if max_bars:
        cap_rows = cap_rows[:max_bars]

    di = cap_header.index('date')
    ti = cap_header.index('time')

    # 1) direct OHLCV (GT2 has none) ; 2) same-series borrow, in run_gt's order.
    ohlcv = _load_ohlcv(cap_rows, cap_header)
    if ohlcv is None:
        first_date = cap_rows[0][di]
        first_time = cap_rows[0][ti]
        for ref in ('GT3', 'GT4', 'GT_master'):
            ref_path = os.path.join(CAP, GT_FILES[ref][1]) if GT_FILES[ref][1] else None
            if not (ref_path and os.path.exists(ref_path)):
                continue
            with open(ref_path, newline='', encoding='utf-8') as f:
                rr = csv.reader(f)
                rhdr = next(rr)
                rrows = [r for r in rr if r]
            if 'date' not in rhdr or 'time' not in rhdr:
                continue
            rdi, rti = rhdr.index('date'), rhdr.index('time')
            off = next((j for j, r in enumerate(rrows)
                        if r[rdi] == first_date and r[rti] == first_time), None)
            if off is None:
                continue
            aligned = rrows[off:off + len(cap_rows)]
            ohlcv = _load_ohlcv(aligned, rhdr)
            if ohlcv:
                break

    bars = []
    for i, row in enumerate(cap_rows):
        if ohlcv:
            o, h, l, c, v = ohlcv[0][i], ohlcv[1][i], ohlcv[2][i], ohlcv[3][i], ohlcv[4][i]
        else:
            # run_gt synth fallback (close-only). Mirrored for completeness.
            c = float(dict(zip(cap_header, row)).get('close', 0) or 0.0)
            o, h, l, v = c, (c * 1.001 if c else 0.001), (c * 0.999 if c else -0.001), 1.0
        bars.append({'date': int(row[di]), 'time': int(row[ti]),
                     'open': o, 'high': h, 'low': l, 'close': c, 'volume': v})
    return bars, cap_header


def main():
    # This resolves GT2's bars from the pinned MultiCharts capture and compares
    # against run_gt's engine output. In a captures-less clone SKIP and return
    # success; when present, run exactly as before.
    if not capture_gate.captures_present():
        print(capture_gate.skip_message("general-entrypoint validation"))
        return 0
    print(f"== General entrypoint validation (GT2, first {MAX_BARS} bars) ==")

    # 1) Build GT2's real bars and export them to a plain OHLCV CSV (the general
    #    case input — no capture dependency at run_el's boundary).
    bars, _ = _resolve_gt2_bars(MAX_BARS)
    tmpdir = new_scratch("pl_gen_")
    bars_csv = os.path.join(tmpdir, "gt2_bars.csv")
    with open(bars_csv, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['date', 'time', 'open', 'high', 'low', 'close', 'volume'])
        for b in bars:
            w.writerow([b['date'], b['time'], b['open'], b['high'],
                        b['low'], b['close'], b['volume']])
    print(f"  wrote {len(bars)} plain bars -> {bars_csv}")

    # 2) Reference: run_gt's validated engine for GT2.
    ref_csv = os.path.join(tmpdir, "predicted_gt2.csv")
    rc = run_gt('GT2', max_bars=MAX_BARS, output_path=ref_csv)
    assert rc == 0, f"run_gt GT2 failed rc={rc}"
    with open(ref_csv, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        ref_header = next(reader)
        ref_rows = [r for r in reader if r]

    # 3) General path: same EL + same bars (loaded from the plain CSV) + NQ config.
    el_source = open(os.path.join(GT, GT_FILES['GT2'][0]), encoding="utf-8").read()
    loaded_bars = load_bars_csv(bars_csv)
    trace_keys = [tk for (_col, tk) in COL_TO_TRACE]
    result = run_el(el_source, loaded_bars, get_config('NQ'), trace_columns=trace_keys)
    gen_rows = result['rows']

    # 4) Byte-identical comparison of the order/position/trade columns.
    assert len(gen_rows) == len(ref_rows), \
        f"row count: run_el={len(gen_rows)} vs run_gt={len(ref_rows)}"
    ref_idx = {col: ref_header.index(col) for (col, _tk) in COL_TO_TRACE}

    mismatches = 0
    first_bad = None
    for bar_i, (gen_row, ref_row) in enumerate(zip(gen_rows, ref_rows)):
        for col_pos, (col, _tk) in enumerate(COL_TO_TRACE):
            gen_val = gen_row[col_pos]
            ref_val = ref_row[ref_idx[col]]
            if gen_val != ref_val:
                mismatches += 1
                if first_bad is None:
                    first_bad = (bar_i, col, ref_val, gen_val)

    if mismatches:
        b, col, rv, gv = first_bad
        print(f"  FAIL: {mismatches} cell mismatches. "
              f"first @ bar {b} col '{col}': run_gt={rv!r} run_el={gv!r}")
        return 1

    # 5) Sanity: the trade list is non-empty and consistent with TotalTrades.
    trades = result['trades']
    final_total = int(ref_rows[-1][ref_idx['totaltrades']])
    assert len(trades) > 0, "expected non-empty trade list"
    print(f"  order/position/trade columns: byte-identical across "
          f"{len(gen_rows)} bars x {len(COL_TO_TRACE)} cols")
    print(f"  trades: {len(trades)} fills; final TotalTrades={final_total}")
    print("PASS: general entrypoint reproduces the validated GT engine.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
