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
pl_oracle.py — exact trade-list digest (the merge gate for perf work): the command-line front end.

The library code lives in ``pl_transpiler.oracle``; this module parses arguments
and calls it. The library names are re-exported here, so
``from pl_transpiler.tools.pl_oracle import ...`` keeps working.

CLI:
    python -m pl_transpiler.tools.pl_oracle <el_file> <bars_csv> [--config NQ] \\
        [--from-date ELINT | --from-bar N] [--to-date ELINT] [--seed-warmup] \\
        [--data2 CSV] [--lenient] [-o digest.txt] [--compare FILE] [--dump FILE] \\
        [--fills-csv FILE] [--digest-v1]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import sys
from collections.abc import Sequence
from typing import Optional, Union

from pl_transpiler.lookback import note_warmup
from pl_transpiler.oracle import (
    BUY_SIDE,
    DIGEST_VERSION,
    FILLS_CSV_COLUMNS,
    SELL_SIDE,
    canonical_fill_line,
    count_round_turns,
    data2_closes_from_bars,
    fill_trade_types,
    fills_sha256,
    format_digest,
    format_summary,
    hashed_payload,
    run_oracle,
    trade_summary,
    window_bars,
    write_fills_csv,
)
from pl_transpiler.run import load_bars, unimplemented_stop_message
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError, set_strict_mode
from pl_transpiler.runtime.accel import (
    AccelUnavailable,
    accel_available,
    explain_native,
    warn_python_path,
)
from pl_transpiler.runtime.bar_tape import ohlcv_columns
from pl_transpiler.tools import _cli

# The library names, re-exported for the old import path.
__all__ = [
    "BUY_SIDE",
    "DIGEST_VERSION",
    "FILLS_CSV_COLUMNS",
    "SELL_SIDE",
    "canonical_fill_line",
    "count_round_turns",
    "data2_closes_from_bars",
    "fill_trade_types",
    "fills_sha256",
    "format_digest",
    "format_summary",
    "hashed_payload",
    "run_oracle",
    "trade_summary",
    "window_bars",
    "write_fills_csv",
]


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pl_oracle",
        description="Run an EasyLanguage strategy over a file of price bars and "
                    "print a short fingerprint (digest) of every trade it makes. "
                    "Two runs with the same digest made exactly the same trades, "
                    "so use it to check that results have not changed "
                    "(see --compare).")
    ap.add_argument("el_file", help="EL/PowerLanguage source file.")
    ap.add_argument("bars_csv", nargs="?", default=None,
                    help="plain OHLCV CSV (date,time,open,high,low,close,volume) "
                         "or a .pbar made by "
                         "python -m pl_transpiler.runtime.bar_tape csv2pbar.")
    ap.add_argument("--config", default="NQ",
                    help="instrument config: a built-in name (NQ, ES) or a path to a "
                         "JSON file with the same fields as a built-in config "
                         "(default: NQ).")
    window = ap.add_mutually_exclusive_group()
    window.add_argument("--from-date", dest="from_date", default=None, type=int,
                        help="first date to include, as an EL date (1YYMMDD: "
                             "1240102 is 2 Jan 2024). Earlier bars are dropped "
                             "unless --seed-warmup.")
    window.add_argument("--from-bar", dest="from_bar", default=None, type=int,
                        help="first bar number to include (0 = first bar). Earlier "
                             "bars are dropped unless --seed-warmup. Cannot be "
                             "combined with --from-date.")
    ap.add_argument("--to-date", dest="to_date", default=None, type=int,
                    help="last date to include, in the same form as --from-date. "
                         "Later bars are dropped.")
    ap.add_argument("--seed-warmup", dest="seed_warmup", action="store_true",
                    help="run the bars before --from-date / --from-bar with fills "
                         "turned off, so Variables, Once blocks and past-bar "
                         "values are set up, instead of dropping them.")
    _cli.add_date_format_arg(ap)
    _cli.add_max_bars_back_arg(ap)
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="hide the warm-up note printed when a strategy reads "
                         "past bars, --max-bars-back is not set and auto "
                         "cannot find the lookback (other "
                         "warnings still print).")
    _cli.add_max_entries_per_direction_arg(ap)
    ap.add_argument("--data2", default=None,
                    help="a second bars file (CSV or .pbar) for Data2, with one "
                         "bar for each bar of the main file. The Python path "
                         "reads its close; the native kernel gets every column.")
    ap.add_argument("--native", dest="use_native", action="store_true", default=None,
                    help="use the Rust kernel (the DEFAULT when it is installed); "
                         "falls back to Python loudly when it cannot serve the run.")
    ap.add_argument("--python", dest="use_native", action="store_false",
                    help="force the pure-Python path (reference / debugging).")
    ap.add_argument("--lenient", action="store_true",
                    help="keep going instead of stopping when the strategy needs "
                         "something PowerBridge does not support yet (same as "
                         "PL_STRICT=0): an unimplemented builtin returns 0 (or '' "
                         "for a text builtin), a missing bar column such as Ticks "
                         "reads as 0, and a 'from entry' exit with several named "
                         "entries open closes the whole position. A WARNING at the "
                         "end names each one; results that depend on them may not "
                         "match MultiCharts.")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write digest here as well as stdout.")
    ap.add_argument("--compare", default=None,
                    help="exit 1 if the produced digest differs from this file.")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v: also print the 'accel not installed' note and how many "
                         "bars skipped fill processing to stderr. -vv: also print "
                         "native-kernel internals ([accel] lines, e.g. the skip-region "
                         "plan); PB_DEBUG=1 does the same for those.")
    ap.add_argument("--fills-csv", dest="fills_csv", default=None,
                    help="write a readable fill log CSV with a position-aware "
                         "trade_type column (EntryLong/ExitLong/EntryShort/ExitShort).")
    ap.add_argument("--summary", action="store_true",
                    help="after the run, print a performance summary of the "
                         "closed trades (net profit, trades, winners/losers, "
                         "win rate, gross profit/loss, max drawdown) to stderr; "
                         "the digest on stdout is unchanged.")
    _cli.add_version(ap, "pl_oracle")
    adv = ap.add_argument_group(
        "advanced options",
        "for maintainers and drawing checks; most users do not need these.")
    adv.add_argument("--dump", default=None,
                    help="write the hashed payload (version line + canonical fill lines) here. "
                         "Their first field is the buy/sell side, not a position label.")
    adv.add_argument("--drawings-json", default=None,
                    help="write a powerbridge-drawing-dump v1 file. Requires "
                         "--drawing-case. Does not change the digest on stdout.")
    adv.add_argument("--drawing-case", default=None,
                    help="case id stored in --drawings-json "
                         "(g-<family>-<NNN>-<slug>).")
    adv.add_argument("--tape-name", default=None,
                    help="name of the bars stored in the drawing dump "
                         "(default: the bars file name without its extension).")
    adv.add_argument("--tape-sha", default=None,
                    help="64-hex sha256 of the bars the dump is compared "
                         "against (default: sha256 of the bars file bytes, "
                         "decompressed when the path ends in .gz).")
    adv.add_argument("--digest-v1", dest="digest_v1", action="store_true",
                    help="emit the pre-v2 digest (for pinned v1 digests): no "
                         "reversal open legs or risk exits. Python path only.")
    adv.add_argument("--explain-native", dest="explain_native", action="store_true",
                    help="list every construct the native kernel would refuse in "
                         "EL_FILE, one 'kind name line' per line sorted by line, "
                         "then exit 0 without running anything (no bars CSV "
                         "needed). A compile-time check: a run can still be "
                         "refused at run time. Exits 2 if the native kernel is not installed.")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    if args.lenient:
        set_strict_mode(False)
    try:
        return _cli.run("pl_oracle", _main, ap, args)
    except UnimplementedBuiltinError as exc:
        print(unimplemented_stop_message("pl_oracle", exc, args.el_file), file=sys.stderr)
        return 2


def _file_sha256(path: Union[str, os.PathLike[str]]) -> str:
    """sha256 of a bars file. ``.gz`` is hashed after decompression."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if str(path).endswith(".gz"):
        raw = gzip.decompress(raw)
    return hashlib.sha256(raw).hexdigest()


def _drawing_tape_arg(args: argparse.Namespace) -> dict[str, str]:
    name = args.tape_name
    if not name:
        stem = os.path.basename(args.bars_csv)
        for suffix in (".csv.gz", ".txt.gz", ".gz", ".csv", ".txt", ".pbar"):
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        name = stem
    sha = args.tape_sha or _file_sha256(args.bars_csv)
    return {"name": name, "sha256": sha}


_EXPLAIN_NOTE = ("compile-time check only; a native run can still be "
                 "refused at run time")


def _explain_native(el_file: str) -> int:
    """``--explain-native``: print each native refusal, run nothing."""
    el_source = open(el_file, encoding="utf-8").read()
    try:
        refusals = explain_native(el_source)
    except AccelUnavailable as exc:
        print(f"pl_oracle: {exc}", file=sys.stderr)
        return 2
    if not refusals:
        print(f"native kernel can run this program ({_EXPLAIN_NOTE})")
        return 0
    for refusal in sorted(refusals, key=lambda r: r.line):
        print(f"{refusal.kind} {refusal.name} {refusal.line}")
    print(f"pl_oracle: {_EXPLAIN_NOTE}", file=sys.stderr)
    return 0


def _main(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.explain_native:
        return _explain_native(args.el_file)
    if args.bars_csv is None:
        ap.error("a bars CSV is required: pl_oracle <el_file> <bars_csv> ...")
    if args.drawings_json and not args.drawing_case:
        ap.error("--drawing-case is required with --drawings-json")

    el_source = open(args.el_file, encoding="utf-8").read()
    mbb_note = _cli.check_max_bars_back(args, el_source)
    bars = _cli.bars_or_error(load_bars, args.bars_csv, args.date_format)
    data2_bars = _cli.bars_or_error(load_bars, args.data2, args.date_format) if args.data2 else None
    data2_closes = ([float(b["close"]) for b in data2_bars]
                    if data2_bars is not None else None)
    config = _cli.config_or_error(args.config)
    output = _cli.output_path(args.output)
    if args.verbose >= 2:
        os.environ["PB_ACCEL_VERBOSE"] = "1"

    result = run_oracle(
        el_source, bars, config,
        data2_closes=data2_closes,
        data2_bars=data2_bars,
        from_date=args.from_date,
        to_date=args.to_date,
        from_bar=args.from_bar,
        seed_warmup=args.seed_warmup,
        use_native=(accel_available() if args.use_native is None else args.use_native),
        digest_version=1 if args.digest_v1 else 2,
        max_bars_back=args.max_bars_back,
        max_entries_per_direction=args.max_entries_per_direction,
        drawings=args.drawings_json is not None,
        drawing_case_id=args.drawing_case,
        drawing_tape=_drawing_tape_arg(args) if args.drawings_json else None,
    )
    if not result.get("native"):
        if args.use_native is False or args.digest_v1:
            pass  # explicitly requested
        elif not accel_available():
            # Expected for the default pure-Python install: only on request.
            if args.verbose:
                warn_python_path()
        elif not (result.get("native_fallback_reason") or "").startswith(
                "native path refused"):
            # A refusal was already reported once by run_oracle.
            warn_python_path(result.get("native_fallback_reason") or "native kernel refused")
    text = result["text"]

    sys.stdout.write(text)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)

    if args.drawings_json:
        from pl_transpiler.runtime.drawing_dump import canonical
        with open(args.drawings_json, "wb") as fh:
            fh.write(canonical(result["drawings"]))
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            f.write(hashed_payload(result["records"], 1 if args.digest_v1 else 2))
    if args.fills_csv:
        write_fills_csv(args.fills_csv, result["records"], tick=config.tick)

    # After the run, so a failing run still prints a single error line.
    note_warmup(el_source, args.max_bars_back, "pl_oracle",
                quiet=args.quiet or not args.max_bars_back_fallback,
                n_bars=ohlcv_columns(bars)["n"])
    warmup_note = (f" ({result['n_warmup']} warmup)" if result.get("n_warmup") else "")
    path_note = ("native" if result.get("native") else "python")
    print(f"[pl_oracle] {result['n_bars']} bars{warmup_note}, {result['fills']} fills, "
          f"{result['round_turns']} round-turns{mbb_note} [{path_note}]"
          + (f" -> {output}" if output else ""), file=sys.stderr)
    if args.verbose and result.get("fast_path_hits") is not None:
        print(f"[pl_oracle] {result['fast_path_hits']} bars had no pending orders, "
              "position or risk exits and skipped fill processing", file=sys.stderr)
    if args.summary:
        sys.stderr.write(format_summary(
            trade_summary(result["records"], config.bigpointvalue)))

    if args.compare:
        want = open(args.compare, encoding="utf-8").read()
        if want != text:
            print("pl_oracle: digest mismatch against --compare file",
                  file=sys.stderr)
            print("--- produced ---", file=sys.stderr)
            print(text, end="", file=sys.stderr)
            print("--- expected ---", file=sys.stderr)
            print(want, end="", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
