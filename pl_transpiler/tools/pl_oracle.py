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
pl_oracle.py — exact trade-list digest (the merge gate for perf work).

Runs EL over a tape through ``run_bar_loop`` with ``persist=True`` and min-emit
(no ``_trace`` stores, no retained rows, no ``inject_trace`` position-column
writes) and prints a canonical digest:

    powerbridge-trade-digest v1
    fills: N
    round_turns: M
    sha256: <hex>

The sha256 is the SHA-256 of the ordered fill tuples
``(type, signal, date, time, price, contracts)``, one canonical line per fill.
Two runs on unchanged code produce byte-identical digest text. Later tickets
must cite the before/after digest — no digest, no merge.

This module does not change the engine, the dialect, or ``run_el``. It is a
correctness instrument, not an optimisation.

Library:
    from pl_transpiler.tools.pl_oracle import run_oracle, format_digest
    d = run_oracle(el_source, bars, config)
    print(d['text'])

CLI:
    python -m pl_transpiler.tools.pl_oracle <el_file> <bars_csv> [--config NQ] \\
        [--from-date ELINT | --from-bar N] [--to-date ELINT] [--seed-warmup] \\
        [--data2 CSV] [-o digest.txt] [--compare FILE] [--dump FILE]
"""
import argparse
import contextlib
import hashlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.engine import FillEngine, run_bar_loop  # noqa: E402
from pl_transpiler.runtime.accel import accel_available, warn_python_path  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    AccelUnavailable,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.runtime.pl_runtime import reset_indicator_caches  # noqa: E402
from pl_transpiler.runtime.bar_tape import BarTape, ohlcv_columns  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars, resolve_trade_from  # noqa: E402

DIGEST_VERSION = "v1"

# Headline Audit Ref 3 Config C audit (Audit_Ref_3_Audit2.EL over the 2,322,973-bar NQ
# tape, persist=True min-emit). Full tape and the 2018-01-01 window (856,914
# bars) produce this identical digest. This module does not load that private
# tape; later tickets cite these constants. No digest, no merge.
AUDIT_REF_3_FILLS = 7364
AUDIT_REF_3_ROUND_TURNS = 3682
AUDIT_REF_3_SHA256 = "d839e7ab8a1feb8a9e238174538f94a477d9c54193a4f48be68b6bd3284d7e0c"
AUDIT_REF_3_DIGEST = (
    "powerbridge-trade-digest v1\n"
    f"fills: {AUDIT_REF_3_FILLS}\n"
    f"round_turns: {AUDIT_REF_3_ROUND_TURNS}\n"
    f"sha256: {AUDIT_REF_3_SHA256}\n"
)


def canonical_fill_line(fill):
    """One stable line for one FillEngine fill / get_mc_trades() dict."""
    return (
        f"{fill['type']}\t{fill['signal']}\t{fill['date']}\t{fill['time']}\t"
        f"{float(fill['price']).hex()}\t{int(fill['contracts'])}"
    )


def fills_sha256(fills):
    """SHA-256 of the ordered canonical fill lines (utf-8). Empty -> SHA-256('')."""
    payload = "".join(canonical_fill_line(f) + "\n" for f in fills)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_digest(*, fills, round_turns, sha256):
    """Canonical four-line digest text. Always ends with a newline."""
    return (
        f"powerbridge-trade-digest {DIGEST_VERSION}\n"
        f"fills: {fills}\n"
        f"round_turns: {round_turns}\n"
        f"sha256: {sha256}\n"
    )


def data2_closes_from_bars(bars, data2_bars):
    """Derive a Data1-aligned Close-of-Data2 series from a Data2 bar tape.

    Aligns on exact ``(date, time)``, last-wins — the same join the native
    kernel uses in ``align_data2``. A Data1 bar with no Data2 match falls back
    to that bar's Data1 close, matching both the kernel's ``DataOf`` missing
    path and codegen's ``kwargs.get('data2_close', close[-1])``.

    ``bars`` / ``data2_bars`` may be ``list[dict]`` or ``BarTape``. The result
    is a ``list[float]`` of length ``len(bars)``, ready for the Python path's
    ``data2_closes`` argument.
    """
    d1 = ohlcv_columns(bars)
    d2 = ohlcv_columns(data2_bars)
    by_key = {}
    dates2, times2, closes2 = d2["date"], d2["time"], d2["close"]
    for j in range(d2["n"]):
        by_key[(int(dates2[j]), int(times2[j]))] = float(closes2[j])
    dates1, times1, closes1 = d1["date"], d1["time"], d1["close"]
    out = [0.0] * d1["n"]
    for i in range(d1["n"]):
        hit = by_key.get((int(dates1[i]), int(times1[i])))
        out[i] = float(closes1[i]) if hit is None else hit
    return out


def window_bars(bars, from_date=None, to_date=None, extra=None):
    """Inclusive EL-date slice of ``bars`` and an optional parallel ``extra`` list.

    ``from_date`` / ``to_date`` are EL integers (YYYMMDD). None means unbounded
    on that side. This is a tape slice (drops the prefix). Engine-level seeded
    warmup is ``run_oracle(..., seed_warmup=True)`` / ``--seed-warmup``.
    """
    if extra is not None and len(extra) != len(bars):
        raise ValueError(
            f"extra series length {len(extra)} != bars length {len(bars)}")
    if from_date is None and to_date is None:
        return bars, extra
    lo = from_date if from_date is not None else -10 ** 18
    hi = to_date if to_date is not None else 10 ** 18
    n = len(bars)
    if isinstance(bars, BarTape):
        dates = bars.date
        keep = [i for i in range(n) if lo <= int(dates[i]) <= hi]
    else:
        keep = [i for i, b in enumerate(bars) if lo <= int(b["date"]) <= hi]
    if not keep:
        empty = bars[0:0]
        out_extra = extra[0:0] if extra is not None else None
        return empty, out_extra
    first, last = keep[0], keep[-1] + 1
    if last - first == len(keep):
        sl = slice(first, last)
        out_extra = extra[sl] if extra is not None else None
        return bars[sl], out_extra
    out_bars = [bars[i] for i in keep]
    out_extra = [extra[i] for i in keep] if extra is not None else None
    return out_bars, out_extra


def _run_oracle_native(el_source, bars, config, *, data2_bars, from_date,
                       to_date, from_bar, seed_warmup):
    """Attempt the native kernel path for one ``run_oracle`` call.

    The native path serves exactly the full-tape, non-windowed shape — the
    kernel's ``run_program`` signature takes no bar window. Data2 is served by
    forwarding the caller's full ``(date, time)``-aligned ``data2_bars`` tape;
    the closes-only ``data2_closes`` argument cannot be reconstructed into that
    tape, so a Data2 program run without ``data2_bars`` is left for the kernel
    to refuse (loudly) and the Python path then runs. Any windowed run is the
    Python path's job.

    Returns ``(digest, None)`` on a successful native run. On any fallback —
    a windowed shape, an absent extension, or a kernel refusal — returns
    ``(None, reason)``; a refusal also prints a LOUD stderr note, so the caller
    never silently receives a partial or approximate trade list.
    """
    if (from_date is not None or to_date is not None or from_bar is not None
            or seed_warmup):
        return None, "run is windowed (not the native shape)"
    try:
        fills = run_native(el_source, bars, config, data2_bars=data2_bars)
    except (AccelUnavailable, AccelRefused) as exc:
        reason = f"native path refused: {exc}"
        print(
            f"[pl_oracle] {reason}; falling back to the Python path (loud)",
            file=sys.stderr,
        )
        return None, reason
    n = ohlcv_columns(bars)["n"]
    digest = {
        "fills": len(fills),
        "round_turns": len(fills) // 2,
        # The FFI returns the ordered fill list, not the D4 closed-position
        # ledger, so the native path cannot report closed_positions. It is not
        # part of the digest text.
        "closed_positions": None,
        "sha256": fills_sha256(fills),
        "n_bars": n,
        "n_warmup": 0,
        "records": fills,
        "native": True,
    }
    digest["text"] = format_digest(
        fills=digest["fills"],
        round_turns=digest["round_turns"],
        sha256=digest["sha256"],
    )
    return digest, None


def run_oracle(el_source, bars, config, *, data2_closes=None, data2_bars=None,
               from_date=None, to_date=None, from_bar=None, seed_warmup=False,
               use_native=False):
    """Transpile persist=True, run min-emit, return a digest dict.

    el_source     : EL/PowerLanguage source string.
    bars          : list of bar dicts (date, time, open, high, low, close, volume)
                    or a BarTape (same values, column-contiguous).
    config        : InstrumentConfig (use get_config('NQ')).
    data2_closes  : optional list[float] aligned with ``bars`` (Close of Data2).
                    Drives the Python path's per-bar ``data2_close``. The native
                    path cannot reconstruct a full Data2 tape from closes-only
                    and still refuses that shape loudly.
    data2_bars    : optional Data2 bar series (``list[dict]`` or ``BarTape``).
                    The native path forwards it to ``run_native``, which aligns
                    it by ``(date, time)``. The Python path, when
                    ``data2_closes`` is omitted, derives a Close series from it
                    the same way (``data2_closes_from_bars``). Either spelling
                    is accepted on the Python path; ``data2_bars`` is never
                    ignored. When both are supplied, ``data2_closes`` is used
                    as the Python-path Close series (the CLI always passes
                    both, derived from the same file).
    from_date     : optional inclusive EL-date lower bound (YYYMMDD).
    to_date       : optional inclusive EL-date upper bound (YYYMMDD).
    from_bar      : optional 0-based tape-index lower bound. Exclusive with
                    from_date.
    seed_warmup   : False (default, issue #11): drop the prefix (tape slice).
                    True (issue #12): keep the prefix, run it with fills
                    suppressed so Once / lookback / Variables stay primed.
                    A windowed run that disagrees on trades is a failed
                    ticket, not a speed win.
    use_native    : False (default): the pure-Python path, byte-identical to
                    before. True: try the optional native kernel first (one FFI
                    crossing) for the full-tape shape; a Data2 program is
                    served by forwarding ``data2_bars``. If the kernel refuses
                    the program or is not built, fall back to the Python path
                    LOUDLY (a stderr note) — never a partial trade list. The
                    digest is identical whichever path runs.

    Returns a dict with keys:
      text, fills, round_turns, closed_positions, sha256, n_bars, n_warmup,
      records, native, native_fallback_reason
    ``records`` is the get_mc_trades() list (ordered). ``round_turns`` is
    ``len(records) // 2``. ``text`` is format_digest(). ``n_warmup`` is 0
    unless seed_warmup ran a prefix. ``native`` is True iff the native kernel
    produced the digest; ``native_fallback_reason`` is a string when
    ``use_native`` was requested but the Python path ran, else ``None``. On
    the native path ``records`` are the kernel fills (no ``order_no``) and
    ``closed_positions`` is ``None`` (the FFI returns fills, not the
    closed-position ledger — neither is in the digest text).
    """
    fallback_reason = None
    if use_native:
        native_digest, fallback_reason = _run_oracle_native(
            el_source, bars, config, data2_bars=data2_bars,
            from_date=from_date, to_date=to_date, from_bar=from_bar,
            seed_warmup=seed_warmup,
        )
        if native_digest is not None:
            return native_digest

    if from_date is not None and from_bar is not None:
        raise ValueError("from_date and from_bar are mutually exclusive")
    from pl_transpiler.runtime.pl_runtime import pl_unimplemented_reset, pl_unimplemented_warn
    pl_unimplemented_reset()
    trade_from = 0
    n_warmup = 0
    if seed_warmup:
        # Keep the head for warmup; still honour to_date as a tail slice.
        bars, data2_closes = window_bars(
            bars, from_date=None, to_date=to_date, extra=data2_closes)
        trade_from = resolve_trade_from(
            bars, from_date=from_date, from_bar=from_bar)
        n_warmup = trade_from
    else:
        if from_bar is not None:
            cut = resolve_trade_from(bars, from_bar=from_bar)
            bars = bars[cut:]
            if data2_closes is not None:
                data2_closes = data2_closes[cut:]
        bars, data2_closes = window_bars(
            bars, from_date=from_date, to_date=to_date, extra=data2_closes)

    # data2_bars is a (date, time) lookup table, not a positional extra, so it
    # is joined AFTER the Data1 window. Deriving before the slice would require
    # data2_bars to be 1:1 with the unsliced tape.
    if data2_closes is None and data2_bars is not None:
        data2_closes = data2_closes_from_bars(bars, data2_bars)

    reset_indicator_caches()
    py_code = transpile(el_source, persist=True)
    ns = {}
    exec(compile(py_code, "<oracle>", "exec"), ns)
    strategy_fn = ns["strategy"]

    cols = ohlcv_columns(bars)
    n = cols["n"]
    o_arr, h_arr, l_arr = cols["open"], cols["high"], cols["low"]
    c_arr, v_arr = cols["close"], cols["volume"]
    dates, times = cols["date"], cols["time"]

    fill_engine = FillEngine(config=config)
    fill_engine._emit_gta6 = False

    extra = None
    if data2_closes is not None:
        d2 = data2_closes

        def extra(i):
            return {"data2_close": d2[i]}

    def emit_row(_i, _barnumber, _trace):
        return None

    def get_ohlcv(i):
        return (o_arr[i], h_arr[i], l_arr[i], c_arr[i], v_arr[i])

    # Strategy Print(File(...)) would otherwise land on stdout and corrupt the
    # digest. Same discard as run_el.
    with open(os.devnull, "w", encoding="utf-8") as _devnull, \
            contextlib.redirect_stdout(_devnull):
        run_bar_loop(
            strategy_fn,
            n_bars=n,
            get_ohlcv=get_ohlcv,
            dates=dates,
            times=times,
            config=config,
            fill_engine=fill_engine,
            emit_row=emit_row,
            per_bar_extra=extra,
            strict=True,
            trade_from=trade_from,
            emit_trace=False,
        )

    records = fill_engine.get_mc_trades()
    # A round-turn is a matched entry+exit contract. get_mc_trades() emits one
    # record per contract, so fills//2 is the audit count (Audit Ref 3: 7364/3682).
    # len(closed_trades) is closed *positions* (a ladder is one position).
    digest = {
        "fills": len(records),
        "round_turns": len(records) // 2,
        "closed_positions": len(fill_engine.closed_trades),
        "sha256": fills_sha256(records),
        "n_bars": n,
        "n_warmup": n_warmup,
        "fast_path_hits": fill_engine.fast_path_hits,
        "records": records,
        "native": False,
        "native_fallback_reason": fallback_reason,
        "unimplemented": pl_unimplemented_warn('pl_oracle'),
    }
    digest["text"] = format_digest(
        fills=digest["fills"],
        round_turns=digest["round_turns"],
        sha256=digest["sha256"],
    )
    return digest


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="pl_oracle",
        description="Exact trade-list digest: persist=True min-emit run of EL "
                    "over a tape. Prints a canonical digest to stdout.")
    ap.add_argument("el_file", help="EL/PowerLanguage source file.")
    ap.add_argument("bars_csv", nargs="?", default=None,
                    help="plain OHLCV CSV (date,time,open,high,low,close,volume) "
                         "or a .pbar produced by csv_to_pbar.")
    ap.add_argument("--config", default="NQ",
                    help="instrument config name (default: NQ).")
    window = ap.add_mutually_exclusive_group()
    window.add_argument("--from-date", dest="from_date", default=None, type=int,
                        help="inclusive EL-date lower bound (YYYMMDD). Tape slice "
                             "unless --seed-warmup.")
    window.add_argument("--from-bar", dest="from_bar", default=None, type=int,
                        help="0-based tape-index lower bound. Tape slice unless "
                             "--seed-warmup. Mutually exclusive with --from-date.")
    ap.add_argument("--to-date", dest="to_date", default=None, type=int,
                    help="inclusive EL-date upper bound (YYYMMDD). Tape slice only.")
    ap.add_argument("--seed-warmup", dest="seed_warmup", action="store_true",
                    help="run the discarded prefix with fills suppressed "
                         "(Once/lookback/Variables stay primed) instead of "
                         "dropping it. A disagreeing digest is a failed ticket.")
    ap.add_argument("--data2", default=None,
                    help="aligned Data2 OHLCV CSV/.pbar; its close column drives "
                         "data2_close per bar on the Python path, and the full "
                         "tape is forwarded to the native kernel under --native.")
    ap.add_argument("--native", dest="use_native", action="store_true", default=None,
                    help="use the Rust kernel (the DEFAULT when it is installed); "
                         "falls back to Python loudly when it cannot serve the run.")
    ap.add_argument("--python", dest="use_native", action="store_false",
                    help="force the pure-Python path (reference / debugging).")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write digest here as well as stdout.")
    ap.add_argument("--compare", default=None,
                    help="exit 1 if the produced digest differs from this file.")
    ap.add_argument("--dump", default=None,
                    help="write the canonical fill lines (the hashed payload) here.")
    return ap


def main(argv=None):
    ap = _build_parser()
    args = ap.parse_args(argv)
    if args.bars_csv is None:
        ap.error("a bars CSV is required: pl_oracle <el_file> <bars_csv> ...")

    el_source = open(args.el_file, encoding="utf-8").read()
    bars = load_bars(args.bars_csv)
    data2_bars = load_bars(args.data2) if args.data2 else None
    data2_closes = ([float(b["close"]) for b in data2_bars]
                    if data2_bars is not None else None)
    config = get_config(args.config)

    result = run_oracle(
        el_source, bars, config,
        data2_closes=data2_closes,
        data2_bars=data2_bars,
        from_date=args.from_date,
        to_date=args.to_date,
        from_bar=args.from_bar,
        seed_warmup=args.seed_warmup,
        use_native=(accel_available() if args.use_native is None else args.use_native),
    )
    if not result.get("native"):
        if args.use_native is False:
            pass  # explicitly requested
        elif not accel_available():
            warn_python_path()
        else:
            warn_python_path(result.get("native_fallback_reason") or "native kernel refused")
    text = result["text"]

    sys.stdout.write(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            for rec in result["records"]:
                f.write(canonical_fill_line(rec) + "\n")

    warmup_note = (f" ({result['n_warmup']} warmup)" if result.get("n_warmup") else "")
    path_note = ("native" if result.get("native") else "python")
    fast_note = ""
    if result.get("fast_path_hits") is not None:
        fast_note = f", {result['fast_path_hits']} fast-path"
    print(f"[pl_oracle] {result['n_bars']} bars{warmup_note}, {result['fills']} fills, "
          f"{result['round_turns']} round-turns{fast_note} [{path_note}]"
          + (f" -> {args.output}" if args.output else ""), file=sys.stderr)

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
