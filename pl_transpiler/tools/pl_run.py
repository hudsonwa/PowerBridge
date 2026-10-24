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
pl_run.py — General EL -> trades entrypoint: the command-line front end.

The library code lives in ``pl_transpiler.run``; this module parses arguments
and calls it. The library names are re-exported here, so
``from pl_transpiler.tools.pl_run import ...`` keeps working.

CLI:
    python -m pl_transpiler.tools.pl_run <el_file> <bars_csv> [--config NQ] [-o out.csv] \
        [--columns a,b,c] [--from-date ELINT | --from-bar N] [--slice] [--lenient]
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable, Sequence
from typing import Any, Callable, Optional

from pl_transpiler.lookback import note_warmup
from pl_transpiler.run import (
    Bars,
    UnknownTraceColumnError,
    _run_el,
    csv_to_pbar,
    display_row_sink,
    format_trace_cell,
    format_trace_row,
    load_bars,
    load_bars_csv,
    pbar_for,
    resolve_trace_columns,
    resolve_trade_from,
    run_el,
    tick_decimals,
    unimplemented_stop_message,
)
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError, set_strict_mode
from pl_transpiler.runtime.accel import warn_python_path
from pl_transpiler.runtime.bar_tape import ohlcv_columns
from pl_transpiler.tools import _cli

# The library names, re-exported for the old import path.
__all__ = [
    "Bars",
    "UnknownTraceColumnError",
    "csv_to_pbar",
    "display_row_sink",
    "format_trace_cell",
    "format_trace_row",
    "load_bars",
    "load_bars_csv",
    "pbar_for",
    "resolve_trace_columns",
    "resolve_trade_from",
    "run_el",
    "tick_decimals",
    "unimplemented_stop_message",
]


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pl_run",
        description="Run an EasyLanguage strategy over a file of price bars and "
                    "write, for each bar, the strategy's values and position as "
                    "CSV. The instrument (tick size, point value) comes from "
                    "--config.")
    ap.add_argument("el_file", help="EL/PowerLanguage source file.")
    # bars_csv is a positional but optional so a lone el_file gets the rc2
    # 'need a bars CSV' error instead of an argparse usage error.
    ap.add_argument("bars_csv", nargs="?", default=None,
                    help="plain OHLCV CSV (date,time,open,high,low,close,volume) "
                         "or a .pbar made by "
                         "python -m pl_transpiler.runtime.bar_tape csv2pbar.")
    ap.add_argument("--config", default="NQ",
                    help="instrument config: a built-in name (NQ, ES) or a path to a "
                         "JSON file with the same fields as a built-in config "
                         "(default: NQ).")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write predicted per-bar CSV here (default: stdout).")
    _cli.add_date_format_arg(ap)
    ap.add_argument("--columns", default=None,
                    help="comma-separated trace columns to emit (default: all).")
    ap.add_argument("--lenient", action="store_true",
                    help="keep going instead of stopping when the strategy needs "
                         "something PowerBridge does not support yet (same as "
                         "PL_STRICT=0): an unimplemented builtin returns 0 (or '' "
                         "for a text builtin), a missing bar column such as Ticks "
                         "reads as 0, and a 'from entry' exit with several named "
                         "entries open closes the whole position. A WARNING at the "
                         "end names each one; results that depend on them may not "
                         "match MultiCharts.")
    window = ap.add_mutually_exclusive_group()
    window.add_argument("--from-date", dest="from_date", default=None, type=int,
                        help="first date to write, as an EL date (1YYMMDD: "
                             "1240102 is 2 Jan 2024). Default: the whole bars "
                             "file. Earlier bars still run as warm-up unless --slice.")
    window.add_argument("--from-bar", dest="from_bar", default=None, type=int,
                        help="first bar number to write (0 = first bar). Cannot "
                             "be combined with --from-date.")
    ap.add_argument("--slice", dest="slice_tape", action="store_true",
                    help="drop the bars before --from-date / --from-bar instead "
                         "of running them as warm-up. Gives the same trades only "
                         "when pl_oracle's digest agrees.")
    _cli.add_max_bars_back_arg(ap)
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="hide the warm-up note printed when a strategy reads "
                         "past bars, --max-bars-back is not set and auto "
                         "cannot find the lookback (other "
                         "warnings still print).")
    _cli.add_max_entries_per_direction_arg(ap)
    ap.add_argument("--full-precision", dest="full_precision", action="store_true",
                    help="write every float at full precision instead of rounding "
                         "money to cents and prices to the instrument tick.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also print the note that the optional native kernel "
                         "is not installed (other fallback notes always print).")
    ap.add_argument("--native", dest="use_native", action="store_true", default=None,
                    help="use the Rust kernel's per-bar trace (the DEFAULT when it is "
                         "installed); falls back to Python loudly when it cannot "
                         "serve the run. The CSV is byte-identical either way.")
    ap.add_argument("--python", dest="use_native", action="store_false",
                    help="force the pure-Python path (reference / debugging).")
    _cli.add_version(ap, "pl_run")
    adv = ap.add_argument_group(
        "advanced options", "most users do not need these.")
    adv.add_argument("--partial", action="store_true",
                    help="opt-in partial mode: unimplemented constructs become "
                         "execution-time stubs (raise only if evaluated); the stub "
                         "manifest is printed loudly to stderr. NOT FAITHFUL.")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_parser()
    # An unknown flag exits nonzero with a usage message (argparse default).
    args = ap.parse_args(argv)
    if args.lenient:
        set_strict_mode(False)
    try:
        return _cli.run("pl_run", _main, ap, args)
    except UnimplementedBuiltinError as exc:
        print(unimplemented_stop_message("pl_run", exc, args.el_file), file=sys.stderr)
        return 2


def _main(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.bars_csv is None:
        ap.error("a bars CSV is required: pl_run <el_file> <bars_csv> ...")

    columns = ([c.strip() for c in args.columns.split(',') if c.strip()]
               if args.columns else None)

    el_source = open(args.el_file, encoding="utf-8").read()
    mbb_note = _cli.check_max_bars_back(args, el_source)
    bars = _cli.bars_or_error(load_bars, args.bars_csv, args.date_format)
    config = _cli.config_or_error(args.config)
    out_path = _cli.output_path(args.output)

    from pl_transpiler.runtime.accel import accel_available
    use_native = accel_available() if args.use_native is None else args.use_native

    def notify(reason: Optional[str] = None) -> None:
        if args.use_native is False:
            return  # --python: explicitly requested
        if reason is not None and reason.startswith("native path refused"):
            print(f"[pl_run] {reason}; using the pure-Python engine (slower)",
                  file=sys.stderr)
        elif reason is not None and (use_native or accel_available()):
            warn_python_path(reason)
        elif args.verbose:
            # Not installed is the default install: -v only.
            warn_python_path()

    out_f = open(out_path, 'w', newline='', encoding='utf-8') if out_path else sys.stdout
    writer = csv.writer(out_f)
    header_sink: Callable[[Iterable[Any]], Any]
    row_sink: Callable[[Iterable[Any]], Any]
    if args.full_precision:
        header_sink = row_sink = writer.writerow
    else:
        header_sink, row_sink = display_row_sink(writer.writerow, config.tick)

    # Stream formatted rows during the bar loop so 2.32M list[str] rows are
    # not retained (that GC tax inflates the rest of the loop). The library
    # default still retains.
    if columns is not None:
        try:
            result = _run_el(el_source, bars, config, trace_columns=columns,
                             partial=args.partial, header_sink=header_sink,
                             row_sink=row_sink,
                             from_date=args.from_date, from_bar=args.from_bar,
                             seed_warmup=not args.slice_tape,
                             max_bars_back=args.max_bars_back,
                             max_entries_per_direction=args.max_entries_per_direction,
                             use_native=use_native, python_path_notify=notify)
        except UnknownTraceColumnError as exc:
            if out_path:
                out_f.close()
            print(f"pl_run: error: {exc}", file=sys.stderr)
            return 2
    else:
        result = _run_el(el_source, bars, config, trace_columns=None,
                         partial=args.partial,
                         header_sink=header_sink,
                         row_sink=row_sink,
                         from_date=args.from_date, from_bar=args.from_bar,
                         seed_warmup=not args.slice_tape,
                         max_bars_back=args.max_bars_back,
                         max_entries_per_direction=args.max_entries_per_direction,
                         use_native=use_native, python_path_notify=notify)
    if out_path:
        out_f.close()

    # After the run, so a failing run still prints a single error line.
    note_warmup(el_source, args.max_bars_back, "pl_run",
                quiet=args.quiet or not args.max_bars_back_fallback,
                n_bars=ohlcv_columns(bars)["n"])
    warmup_note = (f", {result['n_warmup']} warmup" if result.get("n_warmup") else "")
    # Local import: only the summary line needs the oracle module.
    from pl_transpiler.oracle import count_round_turns
    print(f"[pl_run] {len(bars)} bars{warmup_note}, {result['n_rows']} rows, "
          f"{len(result['trades'])} fills, {count_round_turns(result['trades'])} round-turns"
          f"{mbb_note} [{'native' if result.get('native') else 'python'}]"
          + (f" -> {out_path}" if out_path else ""), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
