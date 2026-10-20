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
    from pl_transpiler.tools.pl_run import run_el, load_bars_csv, load_bars
    out = run_el(el_source, bars, config, trace_columns=[...])
    # out = {'columns': [...], 'rows': [[...], ...], 'trades': [...], 'n_rows': N}
    # Optional row_sink streams formatted rows during the loop and, by default,
    # does not retain them (avoids the GC tax of 2.32M list[str] rows). The CLI
    # uses that path. The default library call still returns list[list[str]].

CLI:
    python -m pl_transpiler.tools.pl_run <el_file> <bars_csv> [--config NQ] [-o out.csv] \
        [--columns a,b,c] [--from-date ELINT | --from-bar N] [--slice]
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
from pl_transpiler.runtime.bar_tape import ohlcv_columns  # noqa: E402


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


def pbar_for(path):
    """Sibling ``.pbar`` path for a CSV path (same stem, ``.pbar`` extension)."""
    return os.path.splitext(str(path))[0] + ".pbar"


def load_bars(path, prefer_pbar=True):
    """Load OHLCV from a CSV or a ``.pbar``.

    A ``.pbar`` is memory-mapped (issue #23): opening one is ~0.006 s against
    ~3.0 s to parse the equivalent 2.3M-bar CSV — 545x — so it is the PREFERRED
    form. Pass the CSV path and the sibling ``<stem>.pbar`` is used when it
    exists and is NOT older than the CSV. A stale sidecar is ignored LOUDLY and
    the CSV is parsed instead; a sidecar is never trusted silently.

    ``PB_NO_PBAR=1`` forces the CSV path (for a byte-for-byte audit of the
    loader). ``csv_to_pbar`` builds the sidecar. Anything not ending in
    ``.pbar`` falls back to ``load_bars_csv`` (unchanged).
    """
    p = str(path)
    if p.endswith(".pbar"):
        from pl_transpiler.runtime.bar_tape import load_pbar
        return load_pbar(p)
    if prefer_pbar and not os.environ.get("PB_NO_PBAR"):
        side = pbar_for(p)
        if os.path.exists(side):
            if os.path.getmtime(side) >= os.path.getmtime(p):
                from pl_transpiler.runtime.bar_tape import load_pbar
                return load_pbar(side)
            print(
                "[pl_run] %s is OLDER than %s — ignoring it and parsing the CSV; "
                "rebuild with csv_to_pbar()" % (os.path.basename(side), os.path.basename(p)),
                file=sys.stderr,
            )
    return load_bars_csv(p)


def csv_to_pbar(csv_path, pbar_path):
    """Write a ``.pbar`` from an OHLCV CSV using the same parse as load_bars_csv."""
    from pl_transpiler.runtime.bar_tape import write_pbar
    return write_pbar(pbar_path, load_bars_csv(csv_path))


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


def format_trace_cell(value):
    """Format one trace value the way the GT harness writes a CSV cell.

    None (missing or explicit) becomes '0'; every other value is str(value).
    """
    return '0' if value is None else str(value)


def format_trace_row(trace, columns):
    """Format one bar's trace dict into a list[str] aligned with columns."""
    get = trace.get
    return [format_trace_cell(get(k)) for k in columns]


def resolve_trade_from(bars, *, from_date=None, from_bar=None):
    """First tape index that may fill / emit. 0 = no window (full tape).

    from_date : inclusive EL-integer (YYYMMDD). First bar with date >= from_date.
    from_bar  : 0-based tape index. Mutually exclusive with from_date.
    If the bound is past the tape, returns len(bars) (all warmup, no trades).
    """
    if from_date is not None and from_bar is not None:
        raise ValueError("from_date and from_bar are mutually exclusive")
    if from_bar is not None:
        idx = int(from_bar)
        if idx < 0:
            raise ValueError(f"from_bar must be >= 0, got {idx}")
        return min(idx, len(bars))
    if from_date is not None:
        lo = int(from_date)
        dates = getattr(bars, "date", None)
        if dates is not None:
            for i in range(len(bars)):
                if int(dates[i]) >= lo:
                    return i
            return len(bars)
        for i, b in enumerate(bars):
            if int(b["date"]) >= lo:
                return i
        return len(bars)
    return 0


def run_el(el_source, bars, config, *, trace_columns=None, partial=False,
           row_sink=None, header_sink=None, retain_rows=None,
           from_date=None, from_bar=None, seed_warmup=True):
    """Transpile and run EL over supplied bars + instrument config.

    el_source     : EL/PowerLanguage source string.
    bars          : list of dicts with keys date,time,open,high,low,close,volume
                    (date as EL int YYMMDD, time as EL int HHMM — see load_bars_csv)
                    or a BarTape (column-contiguous .pbar view; same values).
    config        : InstrumentConfig (the #4 per-run config; use get_config('NQ')).
    trace_columns : optional list of trace keys to emit as output columns. When
                    None, the first bar's trace keys (sorted) define the columns.
    partial       : opt-in partial mode. When True, unimplemented EL keywords
                    become execution-time stubs (raising only if evaluated) and the
                    stub manifest is printed LOUDLY to stderr before the run. The
                    DEFAULT path (partial=False) is byte-identical to before.
    row_sink      : optional callable(row: list[str]) invoked once per emitted bar.
                    Additive. The CLI uses this to stream CSV without retaining.
    header_sink   : optional callable(columns: list[str]) invoked once when the
                    column list is first known (first bar, or not at all if n=0).
    retain_rows   : keep formatted rows in the returned 'rows' list. None (default)
                    means True when row_sink is None, False when a sink is given —
                    so the library default is unchanged and a streaming caller does
                    not also hold 2.32M string rows. Pass True to sink AND retain.
    from_date     : optional inclusive EL-date (YYYMMDD) window start. Default
                    None = full tape. Opt-in; never implied.
    from_bar      : optional 0-based tape index window start. Mutually exclusive
                    with from_date.
    seed_warmup   : when a window is set, run the discarded prefix with fills
                    suppressed so Once / lookback / Variables stay primed
                    (default True). False drops the prefix (tape slice) — only
                    equivalent when the trade-list oracle agrees.

    Returns {'columns': [...], 'rows': [[str, ...], ...], 'trades': [...],
    'n_rows': int, 'n_warmup': int}, where rows are formatted exactly as the GT
    harness formats predicted CSV cells and trades come from
    FillEngine.get_mc_trades(). When retain_rows is False, 'rows' is [] and
    n_rows is still the emit count. n_warmup is the seeded-prefix length (0
    when the tape is sliced or no window is set).
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

    trade_from = 0
    n_warmup = 0
    if from_date is not None or from_bar is not None:
        idx = resolve_trade_from(bars, from_date=from_date, from_bar=from_bar)
        if seed_warmup:
            trade_from = idx
            n_warmup = idx
        else:
            bars = bars[idx:]

    cols = ohlcv_columns(bars)
    n = cols['n']
    o_arr, h_arr, l_arr = cols['open'], cols['high'], cols['low']
    c_arr, v_arr = cols['close'], cols['volume']
    dates, times = cols['date'], cols['time']

    fill_engine = FillEngine(config=config)
    # General single-data path: GTA6 multi-file stat/introspection injection is a
    # capture-specific concern and stays off here.
    fill_engine._emit_gta6 = False

    if retain_rows is None:
        retain_rows = row_sink is None

    rows = []
    cols_holder = {'cols': None}
    n_emitted = [0]

    def get_ohlcv(i):
        return (o_arr[i], h_arr[i], l_arr[i], c_arr[i], v_arr[i])

    def emit_row(i, barnumber, trace):
        cols = cols_holder['cols']
        if cols is None:
            cols = (list(trace_columns) if trace_columns is not None
                    else sorted(trace.keys()))
            cols_holder['cols'] = cols
            if header_sink is not None:
                header_sink(cols)
        row = format_trace_row(trace, cols)
        n_emitted[0] += 1
        if retain_rows:
            rows.append(row)
        if row_sink is not None:
            row_sink(row)

    # Fail-loud: a general entrypoint has no throw-sidecar gate, so any EL surface
    # the runtime cannot reproduce must RAISE (strict) rather than be silently
    # counted. The transpiled strategy's own Print(File(...)) lands on stdout; we
    # discard it here so it can never corrupt the returned data or the CLI's stdout.
    from pl_transpiler.runtime.pl_runtime import pl_unimplemented_reset, pl_unimplemented_warn
    from pl_transpiler.runtime.accel import accel_available, warn_python_path
    if accel_available():
        warn_python_path("per-bar trace rows need it; pl_oracle uses the Rust kernel")
    else:
        warn_python_path()
    pl_unimplemented_reset()
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
            trade_from=trade_from,
        )

    columns = cols_holder['cols']
    if columns is None:
        columns = list(trace_columns) if trace_columns is not None else []
        if header_sink is not None:
            header_sink(columns)
    return {
        'columns': columns,
        'rows': rows,
        'trades': fill_engine.get_mc_trades(),
        'n_rows': n_emitted[0],
        'n_warmup': n_warmup,
        # {keyword: calls} of unimplemented builtins this run used as 0/'' (warned on stderr).
        'unimplemented': pl_unimplemented_warn('pl_run'),
    }


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="pl_run",
        description="General EL -> trades entrypoint: run transpiled EL over "
                    "supplied OHLCV bars + an instrument config.")
    ap.add_argument("el_file", help="EL/PowerLanguage source file.")
    # bars_csv is a positional but optional so a lone el_file gives the same rc2
    # 'need a bars CSV' error the hand-rolled walk used to emit.
    ap.add_argument("bars_csv", nargs="?", default=None,
                    help="plain OHLCV CSV (date,time,open,high,low,close,volume) "
                         "or a .pbar produced by csv_to_pbar / "
                         "python -m pl_transpiler.runtime.bar_tape csv2pbar.")
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
    window = ap.add_mutually_exclusive_group()
    window.add_argument("--from-date", dest="from_date", default=None, type=int,
                        help="inclusive EL-date window start (YYYMMDD). Opt-in; "
                             "default is the full tape. Seeded warmup unless --slice.")
    window.add_argument("--from-bar", dest="from_bar", default=None, type=int,
                        help="0-based tape-index window start. Opt-in. "
                             "Mutually exclusive with --from-date.")
    ap.add_argument("--slice", dest="slice_tape", action="store_true",
                    help="drop the prefix instead of seeding warmup. Only "
                         "equivalent when the trade-list oracle agrees.")
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
    bars = load_bars(args.bars_csv)
    config = get_config(args.config)
    out_path = args.output
    out_f = open(out_path, 'w', newline='', encoding='utf-8') if out_path else sys.stdout
    writer = csv.writer(out_f)

    # Stream formatted rows during the bar loop so 2.32M list[str] rows are
    # not retained (that GC tax inflates the rest of the loop). The library
    # default still retains.
    if columns is not None:
        writer.writerow(columns)
        result = run_el(el_source, bars, config, trace_columns=columns,
                        partial=args.partial, row_sink=writer.writerow,
                        from_date=args.from_date, from_bar=args.from_bar,
                        seed_warmup=not args.slice_tape)
    else:
        result = run_el(el_source, bars, config, trace_columns=None,
                        partial=args.partial,
                        header_sink=writer.writerow,
                        row_sink=writer.writerow,
                        from_date=args.from_date, from_bar=args.from_bar,
                        seed_warmup=not args.slice_tape)
    if out_path:
        out_f.close()

    warmup_note = (f", {result['n_warmup']} warmup" if result.get("n_warmup") else "")
    print(f"[pl_run] {len(bars)} bars{warmup_note}, {result['n_rows']} rows, "
          f"{len(result['trades'])} trades"
          + (f" -> {out_path}" if out_path else ""), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
