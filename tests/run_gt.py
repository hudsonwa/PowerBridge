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
run_gt.py — Transpile a GT script with trace mode and execute bar-by-bar
to produce a predicted CSV matching the capture header.

Usage:
    python tests/run_gt.py GT1 [--max-bars N]
    python tests/run_gt.py GT1 --output /tmp/predicted.csv [--max-bars N]
"""
import csv, os, sys, importlib.util, json, traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GT = os.path.join(REPO, "ground_truth")
CAP = os.path.join(GT, "captures")
sys.path.insert(0, REPO)

from pl_transpiler import transpile
from pl_transpiler.runtime.pl_runtime import f32, reset_indicator_caches
from pl_transpiler.runtime.instrument_config import InstrumentConfig, NQ, get_config
# The bar-by-bar engine (FillEngine + run_bar_loop) and the EL date/time parsers
# live in the SHIPPED package (pl_transpiler.engine) so the installed pl_run
# console script works from a bare wheel. They are re-imported here as a single
# source (zero duplication); run_gt's public surface -- run_gt.FillEngine,
# run_gt.run_bar_loop, run_gt._parse_el_date, ... -- is unchanged, so every
# existing caller and monkeypatcher keeps resolving them off this module.
from pl_transpiler.engine import (  # noqa: E402  (re-export, not shadowing)
    FillEngine,
    run_bar_loop,
    _parse_el_date,
    _parse_el_time,
    _el_to_ole,
    _el_date_to_day_of_week,
)

# Map of GT label -> (source_file, capture_file). Single-sourced in
# tests/gt_manifest.py; kept as an INDEPENDENT dict object (a copy) — the reverse
# gates monkeypatch run_gt.GT_FILES and golden_diff.GT_FILES separately and their
# save/restore assumes two distinct objects (aliasing one shared dict would change
# patch semantics).
from tests import gt_manifest  # noqa: E402
GT_FILES = dict(gt_manifest.GT_FILES)

# GTA5-family multi-data scripts: label -> (Data1 file, Data2 file) under CAP.
# Each capture is validated ONLY against the bars from the SAME MC session that
# produced it (the fresh o1-o4 set is the repo @NQ#C/@NQ# restricted to 2024-2025;
# landed under distinct names so it never overwrites the o0/GTA6 data).
GTA5_DATA = {
    "GTA5":    ("@NQ#C 1 Minute.txt",           "@NQ# 1 Minute.txt"),
    "GTA5_o1": ("@NQ#C 1 Minute gta5_1to4.txt", "@NQ# 1 Minute gta5_1to4.txt"),
    "GTA5_o2": ("@NQ#C 1 Minute gta5_1to4.txt", "@NQ# 1 Minute gta5_1to4.txt"),
    "GTA5_o3": ("@NQ#C 1 Minute gta5_1to4.txt", "@NQ# 1 Minute gta5_1to4.txt"),
    "GTA5_o4": ("@NQ#C 1 Minute gta5_1to4.txt", "@NQ# 1 Minute gta5_1to4.txt"),
}

# Column name -> trace key mapping for GT1
# Derived from GT1's Once Print header which defines CSV columns
GT1_COLUMN_MAP = {
    "bn": "_barnumber",
    "date": "_date",
    "time": "_time",
    "close": "_close",
    "ordertag": "ordertag",
    "marketpos": "_market_position",
    "curcontracts": "current_contracts",
    "entryprice": "entry_price",
    "barssinceentry": "bars_since_entry",
    "openposprofit": "open_position_profit",
    "netprofit": "net_profit",
    "totaltrades": "totaltrades",
    "prevclose": "prevclose",
    "counter": "counter",
    "prevcounter": "prevcounter",
    "runsum": "runsum",
    "smamanual": "smamanual",
    "ifelse": "ifelseout",
    "avg": "avgc",
    "xavg": "xavgc",
    "rsi": "rsic",
    "highest": "hih",
    "lowest": "lol",
    "summ": "sumc",
    "stddev": "stddevc",
    "mom": "momc",
    "roc": "rocc",
    "cci": "ccic",
    "atr": "atrc",
    "truerange": "truerangec",
    "medprice": "medprice",
    "range": "rangec",
    "macd": "macdc",
    "absv": "absv",
    "sqr": "sqrv",
    "sqrt": "sqrtv",
    "powr": "powrv",
    "ln": "lnv",
    "exp": "expv",
    "intp": "intp",
    "fracp": "fracp",
    "sign": "sgnv",
    "mod": "modv",
    "maxl": "maxl",
    "minl": "minl",
    "round": "rndv",
    "ceil": "ceilv",
    "floor": "floorv",
    "crossup": "crossupn",
    "crossdn": "crossdnn",
    "strlen": "strlenv",
    "instr": "instrv",
    "lefts": "lefts",
    "rights": "rights",
    "uppers": "uppers",
    # GTA5 specific overrides (columns whose trace key differs from column name)
    "ifelse": "ifelseout",
    "avg": "avgc",
    "atr": "atrc",
    "ad": "adv",
    "calcdate": "calcd",
    "calctime": "calct",
    "marketpos": "_market_position",
    "curcontracts": "current_contracts",
    "entryprice": "entry_price",
    "barssinceentry": "bars_since_entry",
    "openposprofit": "open_position_profit",
    "netprofit": "net_profit",
    "exitprice": "exit_price",
    "barssinceexit": "bars_since_exit",
    "positionprofit": "position_profit",
    "grossprofit": "gross_profit",
    "grossloss": "gross_loss",
    "maxcontractsheld": "maxcontractsheld",
    "maxiddrawdown": "maxiddrawdown",
    # GT4 specific overrides
    "month": "mnv",
    "year": "yrv",
    "dom": "domv",
    "calcdate": "calcd",
    "calctime": "calct",
    "curdate": "curdate",
    "curtime": "curtime",
    "ticks": "ticksv",
    "upticks": "upt",
    "downticks": "dnt",
    "openint": "oiv",
    "ibpcount": "ibpcount",
    "cond1": "cond1n",
    "alwayson": "alwaysonn",
    "repeatout": "repeatout",
    "stochret": "stochret",
    "stochk": "oslowk",
    "stochd": "oslowd",
    "tan45": "tan45",
    "typprice": "typprice",
    "wclose": "wclose",
    "lrslope": "lrslope",
    "lrangle": "lrangle",
    "correl": "correl",
    "adx": "adxv",
    "adxr": "adxrv",
    "dmiplus": "dmip",
    "dmiminus": "dmim",
    "bbup": "bbup",
    "bbdn": "bbdn",
    "closed": "cld",
    "opend": "opd",
    "highd": "hid",
    "lowd": "lod",
    # GT3 specific overrides
    "sine30": "sine30",
    "cos60": "cos60",
    "atan1": "atan1",
    "avgpx": "avgpx",
    "truehi": "truehi",
    "truelo": "truelo",
    "wavg": "wavg",
    "linreg": "linreg",
    "hibar": "hibar",
    "lobar": "lobar",
    "countup": "countup",
    "histhigh": "histhigh",
    "iffup": "iffup",
    "band": "band",
    "notup": "notup",
    "downsum": "downsum",
    "whilesum": "whilesum",
    "dow": "dow",
    "mids": "mids",
    "lowers": "lowers",
    "s2n": "s2n",
}


# Trace keys emitted by FillEngine._inject_gta6_stats + _inject_gta6_introspection.
# Used to decide per-run whether a capture needs GTA6 injection at all: if none of
# these keys are mapped from the capture header, the injection is pure overhead and
# is skipped (it can never change a value that IS in the header).
GTA6_TRACE_KEYS = frozenset({
    # _inject_gta6_stats
    'nwin', 'nlos', 'neven', 'lrgwin', 'lrglos', 'pctprofit', 'maxcwin', 'maxclos',
    'totbwin', 'totblos', 'totbeven', 'avgbwin', 'avgblos', 'avgbeven', 'maxctrprofit',
    'maxposprofit', 'maxposloss', 'maxctr', 'maxentr', 'maxshr', 'maxshrheld', 'maxposago',
    # _inject_gta6_introspection
    'oecount', 'oeprice', 'oeprofit', 'oemaxprofit', 'oeminprofit', 'oedate', 'oetime',
    'oecontracts', 'oeprofitpc', 'oemaxprofitpc', 'oeminprofitpc', 'ennm', 'endate',
    'entime', 'endtm', 'curentr', 'curshr', 'execoff', 'ptcount', 'ptprofit', 'ptsize',
    'ptislong', 'ptisopen', 'ptentrypx', 'ptexitpx', 'ptentrybar', 'ptexitbar',
    'ptentrydtm', 'ptexitdtm', 'ptentrycat', 'ptexitcat', 'ptentrynm', 'ptexitnm',
    'exnm', 'exdate', 'extime', 'exdtm', 'symccy', 'symnm', 'symalt', 'descstr', 'expdate',
})


def build_column_map(capture_header):
    """Build a column_name -> trace_key mapping from capture CSV header.
    
    Uses the GT1 map as base, then falls back to lowercase match for unknown columns.
    """
    col_map = {}
    for col in capture_header:
        col_lower = col.lower().strip()
        # Try GT1 explicit map first
        if col_lower in GT1_COLUMN_MAP:
            col_map[col] = GT1_COLUMN_MAP[col_lower]
        elif col_lower in ('bn', 'date', 'time', 'close'):
            col_map[col] = f"_{col_lower}"
        else:
            # Fallback: use the column name as the variable name (lowercased)
            col_map[col] = col_lower
    return col_map


def _load_ohlcv(cap_rows, cap_header):
    """Extract real OHLCV from a capture that has open/high/low/close/volume columns.
    Returns (o_hist, h_hist, l_hist, c_hist, v_hist) lists, or None if not available."""
    has_ohlcv = all(c in cap_header for c in ('open', 'high', 'low', 'close', 'volume'))
    if not has_ohlcv:
        return None
    o = []
    h = []
    l = []
    c = []
    v = []
    for row in cap_rows:
        rd = dict(zip(cap_header, row))
        try:
            o.append(float(rd.get('open', 0)))
            h.append(float(rd.get('high', 0)))
            l.append(float(rd.get('low', 0)))
            c.append(float(rd.get('close', 0)))
            v.append(float(rd.get('volume', 0)))
        except (ValueError, TypeError):
            return None
    return (o, h, l, c, v)


def _load_multi_data(data1_path, data2_path, max_bars=None, warmup=0):
    """Load Data1 and Data2 from MultiCharts ASCII export files.
    
    Returns (data1_rows, data2_map) where data2_map is a dict of
    (el_date, el_time) -> {open, high, low, close, volume}.
    Data2 is aligned to Data1 by (date, time) with carry-forward.
    """
    data1_rows = []
    with open(data1_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        data1_header = next(reader)  # skip header row
        for row in reader:
            if not row:
                continue
            # Format: Date, Time, Open, High, Low, Close, Volume
            # Date: D/MM/YYYY, Time: HH:MM:SS
            el_date = _parse_el_date(row[0].strip())
            el_time = _parse_el_time(row[1].strip())
            try:
                o, h, l, c, v = float(row[2]), float(row[3]), float(row[4]), float(row[5]), int(row[6])
            except (ValueError, IndexError):
                continue
            data1_rows.append({
                'date': el_date, 'time': el_time,
                'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
            })
    
    # Limit rows
    if max_bars is not None:
        total = max_bars + warmup if warmup else max_bars
        data1_rows = data1_rows[:total]
    
    # Build Data2 lookup map (date,time) -> values, with carry-forward
    data2_map = {}
    with open(data2_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        data2_header = next(reader)  # skip header row
        last_row = None
        for row in reader:
            if not row:
                continue
            el_date = _parse_el_date(row[0].strip())
            el_time = _parse_el_time(row[1].strip())
            key = (el_date, el_time)
            try:
                o, h, l, c, v = float(row[2]), float(row[3]), float(row[4]), float(row[5]), int(row[6])
            except (ValueError, IndexError):
                continue
            data2_map[key] = {
                'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
            }
            last_row = (key, data2_map[key])
    
    # Align Data2 to Data1: for each Data1 bar, find the nearest Data2 at or before
    # the same (date,time). Carry forward the last known Data2 value when exact
    # match is absent (MC behaviour for multi-data).
    data2_aligned = []
    last_data2 = {'open': 0.0, 'high': 0.0, 'low': 0.0, 'close': 0.0, 'volume': 0}
    for d1 in data1_rows:
        key = (d1['date'], d1['time'])
        if key in data2_map:
            last_data2 = data2_map[key]
        data2_aligned.append(dict(last_data2))
    
    return data1_rows, data2_aligned


def run_gt(gt_label, max_bars=None, output_path=None, warmup=0):
    """Transpile GT script with trace mode and run bar-by-bar.
    
    warmup: number of initial bars to pre-process (without writing to output)
            to converge Wilder's-based indicators (RSI, ADX, DMI need ~200+ bars).
    """
    if gt_label not in GT_FILES:
        print(f"Unknown GT label: {gt_label}. Options: {list(GT_FILES.keys())}")
        return 1

    # #7b: clear incremental indicator state so a recycled series id() from a prior
    # label's run can never serve a stale cache entry in this run.
    reset_indicator_caches()

    src_name, cap_name = GT_FILES[gt_label]
    src_path = os.path.join(GT, src_name)
    cap_path = os.path.join(CAP, cap_name) if cap_name else None
    
    if not os.path.exists(src_path):
        print(f"Source not found: {src_path}")
        return 1
    
    if cap_path and not os.path.exists(cap_path):
        print(f"Capture not found: {cap_path}")
        return 1
    
    # Read the capture to get columns and data
    print(f"Reading capture: {cap_path}")
    with open(cap_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        cap_header = next(reader)
        cap_rows = [row for row in reader if row]
    # LastCalc* is the datetime of the FINAL bar of the WHOLE data series (independent
    # of any --max-bars truncation below), so capture the full last row's date/time now.
    full_last_row = cap_rows[-1] if cap_rows else None
    
    # For multi-data scripts (GTA5), load Data1 OHLCV from the text file
    # instead of relying on capture OHLCV, so warmup bars are real pre-capture data.
    data1_ohlcv = None
    data1_warmup_count = 0
    if gt_label in GTA5_DATA:
        _d1_fname = GTA5_DATA[gt_label][0]
        data1_path = os.path.join(CAP, _d1_fname)
        if os.path.exists(data1_path):
            print(f"  Loading Data1: {_d1_fname}")
            data1_raw = []
            with open(data1_path, newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader)  # skip header
                for row in reader:
                    if not row:
                        continue
                    data1_raw.append({
                        'date': _parse_el_date(row[0].strip()),
                        'time': _parse_el_time(row[1].strip()),
                        'open': float(row[2]), 'high': float(row[3]),
                        'low': float(row[4]), 'close': float(row[5]),
                        'volume': int(row[6]),
                    })
            if data1_raw:
                # Build lookup: capture (date,time) -> Data1 OHLCV
                d1_lookup = { (r['date'], r['time']): r for r in data1_raw }
                # Map capture rows to Data1 OHLCV (same length as cap_rows)
                data1_ohlcv = {'o': [], 'h': [], 'l': [], 'c': [], 'v': []}
                for row in cap_rows:
                    rd = dict(zip(cap_header, row))
                    try:
                        key = (int(rd.get('date', 0)), int(rd.get('time', 0)))
                    except (ValueError, TypeError):
                        key = (0, 0)
                    d1r = d1_lookup.get(key)
                    if d1r:
                        data1_ohlcv['o'].append(d1r['open'])
                        data1_ohlcv['h'].append(d1r['high'])
                        data1_ohlcv['l'].append(d1r['low'])
                        data1_ohlcv['c'].append(d1r['close'])
                        data1_ohlcv['v'].append(d1r['volume'])
                    else:
                        # FAIL-LOUD: never source strategy OHLCV from the capture row (the
                        # answer). If a capture (date,time) is absent from raw Data1 input,
                        # alignment has drifted and any "match" would be circular. Alignment
                        # is exact today (0 misses over the full capture); this guards it.
                        raise RuntimeError(
                            f"GTA5 Data1 alignment drift: capture bar (date={key[0]}, "
                            f"time={key[1]}) has no @NQ#C Data1 input row. Refusing to feed "
                            f"capture OHLCV back as strategy input.")
                # Find warmup bars that precede first capture bar
                if warmup > 0 and len(cap_rows) > 0:
                    first_cap = dict(zip(cap_header, cap_rows[0]))
                    try:
                        first_key = (int(first_cap.get('date', 0)), int(first_cap.get('time', 0)))
                    except (ValueError, TypeError):
                        first_key = (0, 0)
                    d1_start = next((j for j, r in enumerate(data1_raw)
                                     if (r['date'], r['time']) == first_key), None)
                    if d1_start is not None:
                        data1_warmup_count = min(warmup, d1_start)
                print(f"    Data1 OHLCV: {len(data1_ohlcv['o'])} bars ({data1_warmup_count} warmup from Data1)")

    if max_bars:
        if warmup > 0 and data1_ohlcv is not None:
            # For GTA5 with Data1, warmup is handled via data1_warmup_count
            # Don't extend cap_rows, just limit them
            cap_rows = cap_rows[:max_bars]
        elif warmup > 0:
            # For other GTs, take extra rows for warmup
            total_rows = warmup + max_bars
            cap_rows = cap_rows[:total_rows]
        else:
            cap_rows = cap_rows[:max_bars]
    
    print(f"  {len(cap_rows)} bars, {len(cap_header)} columns")
    print(f"  Columns: {cap_header}")
    
    # Build column-to-trace-key mapping
    col_map = build_column_map(cap_header)
    
    # Transpile with trace mode
    print(f"Transpiling: {src_path}")
    src = open(src_path, encoding='utf-8').read()
    py_code = transpile(src, trace=True)
    
    # Execute the generated function
    ns = {}
    exec(py_code, ns)
    strategy_fn = ns['strategy']
    
    # Load real OHLCV from capture if available, otherwise search for a matching
    # capture that has OHLCV and aligns by date/time
    ohlcv = _load_ohlcv(cap_rows, cap_header)
    if ohlcv is None and gt_label in ('GT1', 'GT2'):
        # GT1/GT2 captures lack OHLCV; borrow it from a SAME-SERIES capture.
        # A borrowed slice is only valid when the ref's bars align with this
        # capture's bars by (date, time). GT1/GT3/GT4 share one series (start
        # 1240102/852); GT_master/GT2 share another (start .../921). Accepting a
        # blind first-N slice silently misaligns prices (e.g. GT_master into GT1),
        # so we ALWAYS align by the capture's first bar and never blind-slice.
        first_date = cap_rows[0][cap_header.index('date')] if 'date' in cap_header else None
        first_time = cap_rows[0][cap_header.index('time')] if 'time' in cap_header else None
        for ref_label in ('GT3', 'GT4', 'GT_master'):
            ref_cap_path = os.path.join(CAP, GT_FILES[ref_label][1]) if GT_FILES[ref_label][1] else None
            if not (ref_cap_path and os.path.exists(ref_cap_path)):
                continue
            with open(ref_cap_path, newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                ref_header = next(reader)
                ref_rows = [row for row in reader if row]
            if first_date is None or first_time is None or 'date' not in ref_header or 'time' not in ref_header:
                continue
            ref_di, ref_ti = ref_header.index('date'), ref_header.index('time')
            offset = next((j for j, rr in enumerate(ref_rows)
                           if rr[ref_di] == first_date and rr[ref_ti] == first_time), None)
            if offset is None:
                continue  # ref is a different series — do not misalign
            aligned = ref_rows[offset:offset + len(cap_rows)]
            ohlcv = _load_ohlcv(aligned, ref_header)
            if ohlcv:
                print(f"  Using {ref_label} OHLCV for {gt_label} (aligned at offset {offset}, {len(ohlcv[0])} bars)")
                break
    
    # GTA6_2 (file2: stats / trade-introspection) ships NO OHLCV columns, so the
    # strategy needs Data1's bar OHLC sourced from elsewhere. We borrow it from
    # GTA6_1 (file1 of the SAME run), aligned by (date,time). GTA6_1's open/high/
    # low/close/volume columns are the chart's fundamental Data1 INPUTS (NOT
    # strategy outputs), verified byte-identical to the @NQ#C 1 Minute.txt raw feed
    # on the overlap — so this is NOT circular; it is the same same-series-borrow
    # pattern used for GT1/GT2. We use GTA6_1 rather than @NQ#C because the
    # committed @NQ#C export is stale/short: it ends at the GTA5-era cutoff
    # (2026-06-18 14:29) and does NOT cover the GTA6 chart's full session (to
    # 15:15), so at TRUE full range it cannot price the final ~46 bars. GTA6_1
    # carries those bars. Fail-loud on any miss so we never synthesize a price.
    if ohlcv is None and gt_label == 'GTA6_2':
        ref_path = os.path.join(CAP, GT_FILES['GTA6_1'][1])
        if os.path.exists(ref_path):
            print(f"  Loading bar input from GTA6_1 capture OHLCV (file2 has no OHLCV)")
            lookup = {}
            with open(ref_path, newline='', encoding='utf-8') as f:
                rdr = csv.reader(f)
                ref_hdr = next(rdr)
                ri = {name: ref_hdr.index(name) for name in
                      ('date', 'time', 'open', 'high', 'low', 'close', 'volume')}
                for r in rdr:
                    if not r:
                        continue
                    try:
                        k = (int(r[ri['date']]), int(r[ri['time']]))
                        lookup[k] = (float(r[ri['open']]), float(r[ri['high']]),
                                     float(r[ri['low']]), float(r[ri['close']]),
                                     float(r[ri['volume']]))
                    except (ValueError, IndexError):
                        continue
            o = []; h = []; l = []; c = []; v = []
            for row in cap_rows:
                rd = dict(zip(cap_header, row))
                key = (int(rd.get('date', 0)), int(rd.get('time', 0)))
                if key not in lookup:
                    raise RuntimeError(
                        f"GTA6_2 input alignment drift: capture bar (date={key[0]}, "
                        f"time={key[1]}) absent from GTA6_1 OHLCV. Refusing to "
                        f"synthesize prices from the capture under verification.")
                oo, hh, ll, cc, vv = lookup[key]
                o.append(oo); h.append(hh); l.append(ll); c.append(cc); v.append(vv)
            ohlcv = (o, h, l, c, v)

    # Load Data2 for multi-data scripts (GTA5)
    data2_aligned = None
    if gt_label in GTA5_DATA:
        _d2_fname = GTA5_DATA[gt_label][1]
        data2_path = os.path.join(CAP, _d2_fname)
        if os.path.exists(data2_path):
            print(f"  Loading Data2: {_d2_fname}")
            data2_map = {}
            with open(data2_path, newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader)  # skip header
                for row in reader:
                    if not row:
                        continue
                    key = (_parse_el_date(row[0].strip()), _parse_el_time(row[1].strip()))
                    try:
                        data2_map[key] = {
                            'open': float(row[2]), 'high': float(row[3]),
                            'low': float(row[4]), 'close': float(row[5]),
                            'volume': int(row[6]),
                        }
                    except (ValueError, IndexError):
                        continue
            # Align Data2 to cap_rows by (date, time) with carry-forward
            data2_aligned = []
            last_d2 = {'open': 0.0, 'high': 0.0, 'low': 0.0, 'close': 0.0, 'volume': 0}
            for row in cap_rows:
                rd = dict(zip(cap_header, row))
                try:
                    key = (int(rd.get('date', 0)), int(rd.get('time', 0)))
                except (ValueError, TypeError):
                    key = (0, 0)
                if key in data2_map:
                    last_d2 = data2_map[key]
                data2_aligned.append(dict(last_d2))
            print(f"    Data2 aligned: {len(data2_aligned)} bars")
        else:
            print(f"  Data2 file not found: {data2_path}")

    # Run bar-by-bar with growing history
    print("Running bar-by-bar...")
    # Warmup: prepend real pre-capture data from Data1 when available (GTA5)
    PAD = 1
    o_hist, h_hist, l_hist, c_hist = [[0.0] * PAD for _ in range(4)]
    v_hist, d_hist, t_hist = [[0] * PAD for _ in range(3)]
    if data1_warmup_count > 0 and data1_ohlcv is not None:
        # Load warmup Data1 bars from the text file (re-read the first N rows)
        data1_path = os.path.join(CAP, GTA5_DATA[gt_label][0])
        try:
            warmup_bars = []
            with open(data1_path, newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader)
                for j, row in enumerate(reader):
                    if not row:
                        continue
                    el_date = _parse_el_date(row[0].strip())
                    el_time = _parse_el_time(row[1].strip())
                    # Stop after we've collected warmup bars that precede capture start
                    if j < data1_warmup_count:
                        warmup_bars.append({
                            'o': float(row[2]), 'h': float(row[3]),
                            'l': float(row[4]), 'c': float(row[5]),
                            'v': int(row[6]),
                            'date': el_date, 'time': el_time,
                        })
                    else:
                        break
            for wb in warmup_bars:
                o_hist.append(wb['o'])
                h_hist.append(wb['h'])
                l_hist.append(wb['l'])
                c_hist.append(wb['c'])
                v_hist.append(wb['v'])
                d_hist.append(wb['date'])
                t_hist.append(wb['time'])
            print(f"    Prepended {len(warmup_bars)} warmup bars from Data1")
            # For GTA5, warmup pre-data is already in the history; don't skip output.
            # Reset warmup to 0 so the output loop doesn't skip bars.
            saved_warmup = warmup
            warmup = 0
        except Exception as e:
            print(f"    Warmup prepend failed: {e}")
    
    # LastCalc* : derive once from the FINAL bar's date/time (fundamental input series).
    # JDate = days since 1899-12-30 (Excel/OLE serial); time-of-day in min/sec/ms.
    lastcalc_kwargs = {}
    if full_last_row is not None:
        import datetime as _dt
        _lrd = dict(zip(cap_header, full_last_row))
        try:
            _ld = int(_lrd.get('date', 0)); _lt = int(_lrd.get('time', 0))
            _yr = 1900 + _ld // 10000; _mo = (_ld // 100) % 100; _dy = _ld % 100
            _hh = _lt // 100; _mn = _lt % 100
            _jdate = (_dt.date(_yr, _mo, _dy) - _dt.date(1899, 12, 30)).days
            _secs = _hh * 3600 + _mn * 60
            lastcalc_kwargs = {
                'lastcalcjdate': _jdate,
                'lastcalcdatetime': _jdate + _secs / 86400.0,
                'lastcalcmmtime': _hh * 60 + _mn,
                'lastcalcsstime': _secs,
                'lastcalcmstime': _secs * 1000,
            }
        except (ValueError, TypeError):
            lastcalc_kwargs = {}

    # STREAM output: open the writer and emit the header BEFORE the bar loop so
    # predicted rows are written one at a time (writerow per bar) instead of being
    # accumulated in a list that grows to the full 200k+ capture range (an OOM
    # source). Determine the output path up front (was computed after the loop).
    if output_path is None:
        output_path = os.path.join(REPO, f"predicted_{gt_label.lower()}.csv")
    print(f"Writing predicted CSV: {output_path}")
    _out_f = open(output_path, 'w', newline='', encoding='utf-8')
    writer = csv.writer(_out_f)
    writer.writerow(cap_header)
    _strict = bool(os.environ.get("PL_STRICT"))
    # Select the per-run instrument config (all current GT labels are @NQ#C).
    # This object is the single source of the chart/instrument constants that were
    # formerly hard-coded here and in FillEngine — the shared runtime stays
    # instrument-agnostic.
    instr_config = get_config('NQ')
    fill_engine = FillEngine(config=instr_config)
    # Skip GTA6 stat/introspection injection entirely when this capture's header
    # maps to NONE of those trace keys (e.g. GT2) — avoids per-bar dict writes that
    # would never be read into an output column.
    fill_engine._emit_gta6 = bool(set(col_map.values()) & GTA6_TRACE_KEYS)
    # Precompute the date-column index once so SessionLastBar can read the next
    # bar's date directly instead of rebuilding dict(zip(cap_header, ...)) per bar.
    _date_idx = cap_header.index('date') if 'date' in cap_header else None
    _time_idx = cap_header.index('time') if 'time' in cap_header else None
    # Canonical session-end time-of-day: the recurring time at each day boundary
    # (the bar whose NEXT bar starts a new day). The chart's GENUINE final bar has
    # no next row for the day-change proxy to compare, so SessionLastBar classifies
    # it by whether its time equals this value. Derived from the data so it stays
    # instrument-agnostic (no hard-coded 1515).
    _session_end_tm = None
    if _date_idx is not None and _time_idx is not None and len(cap_rows) > 1:
        from collections import Counter
        _se_ctr = Counter()
        for _j in range(len(cap_rows) - 1):
            try:
                if int(cap_rows[_j][_date_idx]) != int(cap_rows[_j + 1][_date_idx]):
                    _se_ctr[int(cap_rows[_j][_time_idx])] += 1
            except (ValueError, TypeError, IndexError):
                continue
        if _se_ctr:
            _session_end_tm = _se_ctr.most_common(1)[0][0]
    # Build the EL date/time series (fundamental inputs) for the shared core.
    bar_dates = []
    bar_times = []
    for row in cap_rows:
        rd = dict(zip(cap_header, row))
        try:
            bar_dates.append(int(rd.get('date', 0)))
        except (ValueError, TypeError):
            bar_dates.append(0)
        try:
            bar_times.append(int(rd.get('time', 0)))
        except (ValueError, TypeError):
            bar_times.append(0)

    def _get_ohlcv(i):
        if data1_ohlcv is not None:
            return (data1_ohlcv['o'][i], data1_ohlcv['h'][i], data1_ohlcv['l'][i],
                    data1_ohlcv['c'][i], data1_ohlcv['v'][i])
        if ohlcv:
            return (ohlcv[0][i], ohlcv[1][i], ohlcv[2][i], ohlcv[3][i], ohlcv[4][i])
        rd = dict(zip(cap_header, cap_rows[i]))
        try:
            c = float(rd.get('close', 0))
        except (ValueError, TypeError):
            c = 0.0
        o = c
        h = c * 1.001 if c else 0.001
        l = c * 0.999 if c else -0.001
        return (o, h, l, c, 1.0)

    def _per_bar_extra(i):
        rd = dict(zip(cap_header, cap_rows[i]))
        ex = {}
        # Capture-sourced INPUT fields. curdate/curtime feed CurrentDate/CurrentTime
        # (the realtime export date/time — distinct from the bar's Date/Time, which
        # flow through the d_hist/t_hist series into Month/Year/DayOfMonth);
        # ticks/upticks/downticks/openint are fundamental tick inputs. NOTE:
        # closed/opend/highd/lowd are deliberately NOT taken from the capture —
        # daily OHLC is computed in run_bar_loop and must win.
        kw_map = {
            'curdate': 'current_date', 'curtime': 'current_time',
            'ticks': 'ticks', 'upticks': 'upticks',
            'downticks': 'downticks', 'openint': 'openint',
        }
        for col_key, val in rd.items():
            col_lc = col_key.lower()
            if col_lc in kw_map:
                try:
                    ex[kw_map[col_lc]] = float(val) if '.' in val else int(val)
                except (ValueError, TypeError):
                    pass
        # Fall back to the bar date/time columns when curdate/curtime are absent.
        if 'current_date' not in ex:
            try:
                ex['current_date'] = int(rd.get('date', 0))
            except (ValueError, TypeError):
                pass
        if 'current_time' not in ex:
            try:
                ex['current_time'] = int(rd.get('time', 0))
            except (ValueError, TypeError):
                pass
        # DateTime (YYYYMMDD.frac) from the CurrentDate/CurrentTime values — matches
        # the original capture-path kwargs assembly.
        if 'current_date' in ex and 'current_time' in ex:
            d = ex['current_date']
            t = ex['current_time']
            yyyy = 1900 + (d // 10000)
            mm = (d // 100) % 100
            dd = d % 100
            date_part = yyyy * 10000 + mm * 100 + dd
            hh = t // 100
            mm_t = t % 100
            ex['datetime'] = date_part + (hh * 3600 + mm_t * 60) / 86400.0
        # Per-bar Len/Prec inputs (GT1 indicator length/precision) from the capture.
        try:
            ex['len'] = int(rd.get('len', 14))
        except (ValueError, TypeError):
            ex['len'] = 14
        try:
            ex['prec'] = int(rd.get('prec', 6))
        except (ValueError, TypeError):
            ex['prec'] = 6
        # Data2 (GTA5 multi-data) aligned by (date,time).
        if data2_aligned is not None and i < len(data2_aligned):
            d2 = data2_aligned[i]
            ex['data2_close'] = d2['close']
            ex['data2_high'] = d2['high']
            ex['data2_low'] = d2['low']
            ex['data2_open'] = d2['open']
        return ex

    _rows_written = [0]

    def _emit_row(i, barnumber, trace):
        row_dict = dict(zip(cap_header, cap_rows[i]))
        pred_row = []
        for col in cap_header:
            key = col_map.get(col, col.lower())
            if col in ('bn', 'date', 'time', 'close', 'open', 'high', 'low', 'volume') and col in row_dict:
                val = row_dict[col]
            elif key in trace:
                vv = trace[key]
                val = str(vv) if vv is not None else '0'
            else:
                val = '0'
            pred_row.append(val)
        writer.writerow(pred_row)
        _rows_written[0] += 1

    init_hist = {'o': o_hist, 'h': h_hist, 'l': l_hist, 'c': c_hist,
                 'v': v_hist, 'd': d_hist, 't': t_hist}

    _throw_count, _total_strategy_calls, _first_traceback = run_bar_loop(
        strategy_fn,
        n_bars=len(cap_rows),
        get_ohlcv=_get_ohlcv,
        dates=bar_dates,
        times=bar_times,
        config=instr_config,
        fill_engine=fill_engine,
        emit_row=_emit_row,
        warmup=warmup,
        init_hist=init_hist,
        per_bar_extra=_per_bar_extra,
        lastcalc_kwargs=lastcalc_kwargs,
        session_end_tm=_session_end_tm,
        strict=_strict,
    )

    _out_f.close()
    if warmup > 0:
        print(f"  Warmup: skipped first {warmup} bars from output")

    print(f"Done: {_rows_written[0]} rows written")

    # FAIL-LOUD: surface the per-bar strategy throw count for this run and write
    # a sidecar JSON next to the output CSV so verify_all can gate on it.
    if _throw_count > 0:
        print(f"WARNING [{gt_label}]: strategy threw on {_throw_count}/"
              f"{_total_strategy_calls} bars. First traceback:\n{_first_traceback}")
    else:
        print(f"  [{gt_label}] strategy throws: 0/{_total_strategy_calls} bars (clean)")
    sidecar = output_path + ".throws.json"
    with open(sidecar, "w", encoding="utf-8", newline="\n") as f:
        json.dump({
            "label": gt_label,
            "throw_count": _throw_count,
            "total_bars": _total_strategy_calls,
            "first_traceback": _first_traceback,
        }, f, indent=2)
    return 0


def read_throws(output_path):
    """Read the throw sidecar written next to a run_gt output CSV.

    Returns the parsed dict, or None if the sidecar is absent."""
    sidecar = output_path + ".throws.json"
    if not os.path.exists(sidecar):
        return None
    with open(sidecar, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    
    gt_label = argv[0]
    max_bars = None
    output = None
    warmup = 0

    i = 1
    while i < len(argv):
        if argv[i] == '--max-bars':
            i += 1
            max_bars = int(argv[i])
        elif argv[i] in ('--output', '-o'):
            i += 1
            output = argv[i]
        elif argv[i] == '--warmup':
            i += 1
            warmup = int(argv[i])
        i += 1

    # The runner iterates a pinned MultiCharts capture. From the CLI, in a
    # captures-less clone SKIP (one-line docs pointer) and return success; when the
    # captures are present, run exactly as before. (The run_gt() function itself is
    # never gated — the reverse gates repoint GT_FILES and call it directly.)
    from tests import capture_gate
    if not capture_gate.captures_present():
        print(capture_gate.skip_message(f"run_gt {gt_label}"))
        return 0

    return run_gt(gt_label, max_bars=max_bars, output_path=output, warmup=warmup)


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv[1:]))
