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
pl_run.py — General EL -> trades entrypoint.

A THIN, reusable wrapper that runs ARBITRARY transpiled PowerLanguage/EasyLanguage
over SUPPLIED OHLCV bars + an instrument config and returns the per-bar trace
columns plus the orders/position/trades list — WITHOUT the capture-coupled
GT_FILES machinery in tests/run_gt.py.

It reuses the SAME bar-by-bar engine + FillEngine as the GT harness via the shared
`run_bar_loop` core (the shipped `pl_transpiler.engine` module, which the GT harness
re-exports), and the per-run instrument constants from the #4 InstrumentConfig —
nothing is hard-coded and nothing is read from a capture. Unimplemented EL still
RAISES inside the runtime (fail-loud).

Library:
    from pl_transpiler.tools.pl_run import run_el, load_bars_csv
    out = run_el(el_source, bars, config, trace_columns=[...])
    # out = {'columns': [...], 'rows': [[...], ...], 'trades': [...]}

CLI:
    python -m pl_transpiler.tools.pl_run <el_file> <bars_csv> [--config NQ] [-o out.csv] \
        [--columns a,b,c]
"""
import argparse
import contextlib
import csv
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.errors import UnimplementedKeywordError, format_error_lines  # noqa: E402
from pl_transpiler.runtime.pl_runtime import reset_indicator_caches  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
# The bar-by-bar engine pieces (FillEngine, run_bar_loop, EL date/time parsers) ship in
# the package (pl_transpiler.engine), so the installed `pl_run` console script drives
# them directly from a bare wheel — no dependency on the private tests/ harness. The GT
# capture harness (tests/run_gt.py) re-exports the same objects, so behaviour is identical.
from pl_transpiler.engine import (  # noqa: E402
    FillEngine, run_bar_loop, _parse_el_date, _parse_el_time,
)


def load_bars_csv(path):
    """Load a plain OHLCV CSV into the bar list run_el expects.

    Header must contain: date,time,open,high,low,close,volume (case-insensitive).
    date/time accept either MultiCharts ASCII export form (DD/MM/YYYY, HH:MM:SS) or
    the EL integer form (YYMMDD, HHMM); both are normalized to EL integers.
    """
    bars = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = [h.strip().lower() for h in next(reader)]
        idx = {name: header.index(name)
               for name in ('date', 'time', 'open', 'high', 'low', 'close', 'volume')}
        for row in reader:
            if not row:
                continue
            bars.append({
                'date': _parse_el_date(row[idx['date']].strip()),
                'time': _parse_el_time(row[idx['time']].strip()),
                'open': float(row[idx['open']]),
                'high': float(row[idx['high']]),
                'low': float(row[idx['low']]),
                'close': float(row[idx['close']]),
                'volume': float(row[idx['volume']]),
            })
    return bars


def _print_partial_manifest(errors):
    """Loud partial-mode stub manifest to stderr: banner + per-stub + warning."""
    n = len(errors)
    print("=" * 70, file=sys.stderr)
    print("PARTIAL TRANSPILE -- NOT FAITHFUL", file=sys.stderr)
    print(f"{n} unimplemented construct(s) replaced with execution-time stubs:",
          file=sys.stderr)
    for txt in format_error_lines(errors):
        print(f"  {txt}", file=sys.stderr)
    print("Each stub raises UnimplementedKeywordError when evaluated -- "
          "do not trust backtest results.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)


def run_el(el_source, bars, config, *, trace_columns=None, partial=False):
    """Transpile and run EL over supplied bars + instrument config.

    el_source     : EL/PowerLanguage source string.
    bars          : list of dicts with keys date,time,open,high,low,close,volume
                    (date as EL int YYMMDD, time as EL int HHMM — see load_bars_csv).
    config        : InstrumentConfig (the #4 per-run config; use get_config('NQ')).
    trace_columns : optional list of trace keys to emit as output columns. When
                    None, the first bar's trace keys (sorted) define the columns.
    partial       : opt-in FL2 partial mode. When True, unimplemented EL keywords
                    become execution-time stubs (raising only if evaluated) and the
                    stub manifest is printed LOUDLY to stderr before the run. The
                    DEFAULT path (partial=False) is byte-identical to before.

    Returns {'columns': [...], 'rows': [[str, ...], ...], 'trades': [...]}, where
    rows are formatted exactly as the GT harness formats predicted CSV cells and
    trades come from FillEngine.get_mc_trades().
    """
    reset_indicator_caches()
    if partial:
        # Surface the manifest loudly before running. Detect the stubs via the
        # strict transpile's collect-all diagnostics (empty when nothing is stubbed).
        try:
            transpile(el_source, trace=True)
        except UnimplementedKeywordError as e:
            _print_partial_manifest(e.errors)
        py_code = transpile(el_source, trace=True, partial=True)
    else:
        py_code = transpile(el_source, trace=True)
    ns = {}
    exec(py_code, ns)
    strategy_fn = ns['strategy']

    n = len(bars)
    o_arr = [float(b['open']) for b in bars]
    h_arr = [float(b['high']) for b in bars]
    l_arr = [float(b['low']) for b in bars]
    c_arr = [float(b['close']) for b in bars]
    v_arr = [float(b['volume']) for b in bars]
    dates = [int(b['date']) for b in bars]
    times = [int(b['time']) for b in bars]

    fill_engine = FillEngine(config=config)
    # General single-data path: GTA6 multi-file stat/introspection injection is a
    # capture-specific concern and stays off here.
    fill_engine._emit_gta6 = False

    rows = []
    cols_holder = {'cols': None}

    def get_ohlcv(i):
        return (o_arr[i], h_arr[i], l_arr[i], c_arr[i], v_arr[i])

    def emit_row(i, barnumber, trace):
        if cols_holder['cols'] is None:
            cols_holder['cols'] = (list(trace_columns) if trace_columns is not None
                                   else sorted(trace.keys()))
        row = []
        for k in cols_holder['cols']:
            if k in trace:
                vv = trace[k]
                row.append(str(vv) if vv is not None else '0')
            else:
                row.append('0')
        rows.append(row)

    # Fail-loud: a general entrypoint has no throw-sidecar gate, so any EL surface
    # the runtime cannot reproduce must RAISE (strict) rather than be silently
    # counted. The transpiled strategy's own Print(File(...)) lands on stdout; we
    # discard it here so it can never corrupt the returned data or the CLI's stdout.
    with open(os.devnull, 'w', encoding='utf-8') as _devnull, contextlib.redirect_stdout(_devnull):
        run_bar_loop(
            strategy_fn,
            n_bars=n,
            get_ohlcv=get_ohlcv,
            dates=dates,
            times=times,
            config=config,
            fill_engine=fill_engine,
            emit_row=emit_row,
            strict=True,
        )

    columns = cols_holder['cols']
    if columns is None:
        columns = list(trace_columns) if trace_columns is not None else []
    return {'columns': columns, 'rows': rows, 'trades': fill_engine.get_mc_trades()}


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="pl_run",
        description="General EL -> trades entrypoint: run transpiled EL over "
                    "supplied OHLCV bars + an instrument config.")
    ap.add_argument("el_file", help="EL/PowerLanguage source file.")
    # bars_csv is a positional but optional so a lone el_file gives the same rc2
    # 'need a bars CSV' error the hand-rolled walk used to emit.
    ap.add_argument("bars_csv", nargs="?", default=None,
                    help="plain OHLCV CSV (date,time,open,high,low,close,volume).")
    ap.add_argument("--config", default="NQ",
                    help="instrument config name (default: NQ).")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write predicted per-bar CSV here (default: stdout).")
    ap.add_argument("--columns", default=None,
                    help="comma-separated trace columns to emit (default: all).")
    ap.add_argument("--partial", action="store_true",
                    help="opt-in partial mode: unimplemented constructs become "
                         "execution-time stubs (raise only if evaluated); the stub "
                         "manifest is printed loudly to stderr. NOT FAITHFUL.")
    return ap


def main(argv=None):
    ap = _build_parser()
    # An unknown flag now exits nonzero with a usage message (argparse default),
    # replacing the old silent-ignore behaviour of the hand-rolled walk.
    args = ap.parse_args(argv)
    if args.bars_csv is None:
        ap.error("a bars CSV is required: pl_run <el_file> <bars_csv> ...")

    columns = ([c.strip() for c in args.columns.split(',') if c.strip()]
               if args.columns else None)

    el_source = open(args.el_file, encoding="utf-8").read()
    bars = load_bars_csv(args.bars_csv)
    config = get_config(args.config)
    result = run_el(el_source, bars, config, trace_columns=columns,
                    partial=args.partial)

    out_path = args.output
    out_f = open(out_path, 'w', newline='', encoding='utf-8') if out_path else sys.stdout
    writer = csv.writer(out_f)
    writer.writerow(result['columns'])
    for row in result['rows']:
        writer.writerow(row)
    if out_path:
        out_f.close()

    print(f"[pl_run] {len(bars)} bars, {len(result['rows'])} rows, "
          f"{len(result['trades'])} trades"
          + (f" -> {out_path}" if out_path else ""), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
