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

    powerbridge-trade-digest v2
    fills: N
    round_turns: M
    sha256: <hex>

The sha256 is the SHA-256 of the line ``digest_version: 2`` followed by the
ordered fill tuples ``(type, signal, date, time, price, contracts)``, one
canonical line per fill. Every change of the signed position is a fill: a
reversal logs its close leg and then its open leg, and SetStopLoss /
SetProfitTarget / other Set* risk exits are logged too. ``round_turns`` is the
number of exit records (each closes one entered contract).

``--digest-v1`` reproduces the pre-v2 digest byte for byte (no version line,
reversal open legs and risk exits missing, ``round_turns = fills // 2``) for
anyone with pinned v1 digests. It always runs the Python path.
The hashed ``type`` field is the order's buy/sell SIDE, not a position label:
``EntryLong`` means a buy-side fill (Buy, BuyToCover, or a risk exit of a
short) and ``ExitLong`` a sell-side fill (Sell, SellShort, or a risk exit of
a long). It is the same in v1 and v2. For the position-aware label
(EntryLong / ExitLong / EntryShort / ExitShort) use ``fill_trade_types`` or
``--fills-csv``; it is not hashed.
Two runs on unchanged code produce byte-identical digest text. Changes that may
affect fills should cite the before/after digest.

This module does not change the engine, the dialect, or ``run_el``. It is a
correctness instrument, not an optimisation.

Library:
    from pl_transpiler.tools.pl_oracle import run_oracle, format_digest
    d = run_oracle(el_source, bars, config)
    print(d['text'])

CLI:
    python -m pl_transpiler.tools.pl_oracle <el_file> <bars_csv> [--config NQ] \\
        [--from-date ELINT | --from-bar N] [--to-date ELINT] [--seed-warmup] \\
        [--data2 CSV] [--lenient] [-o digest.txt] [--compare FILE] [--dump FILE] \\
        [--fills-csv FILE] [--digest-v1]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import gzip
import hashlib
import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from pl_transpiler.runtime.instrument_config import InstrumentConfig
    from pl_transpiler.tools.pl_run import Bars

from pl_transpiler import transpile
from pl_transpiler.engine import FillEngine, run_bar_loop
from pl_transpiler.lookback import (
    check_max_bars_back,
    check_max_entries_per_direction,
    note_warmup,
    warn_short_history,
)
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError, set_strict_mode
from pl_transpiler.runtime.accel import (
    AccelRefused,
    AccelUnavailable,
    accel_available,
    explain_native,
    run_native,
    run_native_drawings,
    warn_python_path,
)
from pl_transpiler.runtime.bar_tape import BarTape, ohlcv_columns
from pl_transpiler.runtime.pl_runtime import reset_indicator_caches
from pl_transpiler.tools import _cli
from pl_transpiler.tools.pl_run import (
    load_bars,
    resolve_trade_from,
    tick_decimals,
    unimplemented_stop_message,
)

DIGEST_VERSION = "v2"

# Headline reference-strategy digest (the reference EL over the 2,322,973-bar NQ
# tape, persist=True min-emit). Full tape and the 2018-01-01 window (856,914
# bars) produce this identical digest. This module does not load that private
# tape; the constants are kept as a fixed reference.
AUDIT_REF_3_FILLS = 7364
AUDIT_REF_3_ROUND_TURNS = 3682
AUDIT_REF_3_SHA256 = "d839e7ab8a1feb8a9e238174538f94a477d9c54193a4f48be68b6bd3284d7e0c"
AUDIT_REF_3_DIGEST = (
    "powerbridge-trade-digest v1\n"
    f"fills: {AUDIT_REF_3_FILLS}\n"
    f"round_turns: {AUDIT_REF_3_ROUND_TURNS}\n"
    f"sha256: {AUDIT_REF_3_SHA256}\n"
)


# Hashed fill ``type`` values. They name the order SIDE, not the position effect.
BUY_SIDE = "EntryLong"
SELL_SIDE = "ExitLong"


def canonical_fill_line(fill):
    """One stable line for one FillEngine fill / get_mc_trades() dict.

    The first field is the buy/sell side (``EntryLong`` = buy side,
    ``ExitLong`` = sell side) exactly as both engines emit it, so a SellShort
    from flat hashes as ``ExitLong``. Do not change it: every pinned digest
    depends on it. See ``fill_trade_types`` for the position-aware label.
    """
    return (
        f"{fill['type']}\t{fill['signal']}\t{fill['date']}\t{fill['time']}\t"
        f"{float(fill['price']).hex()}\t{int(fill['contracts'])}"
    )


def fill_trade_types(fills):
    """Position-aware label for each fill: EntryLong, ExitLong, EntryShort or ExitShort.

    Not part of the digest. Each record is one contract; the position is
    replayed from flat with buy side = +contracts and sell side = -contracts.
    A buy while short is ExitShort, otherwise EntryLong; a sell while long is
    ExitLong, otherwise EntryShort. Because a reversal is logged one contract
    per record, it splits into exit records followed by entry records. Works
    the same on Python and native records, since both carry the same side.
    """
    labels = []
    position = 0
    for fill in fills:
        qty = int(fill["contracts"])
        if fill["type"] == BUY_SIDE:
            labels.append("ExitShort" if position < 0 else "EntryLong")
            position += qty
        elif fill["type"] == SELL_SIDE:
            labels.append("ExitLong" if position > 0 else "EntryShort")
            position -= qty
        else:
            raise ValueError(f"unknown fill side {fill['type']!r}")
    return labels


FILLS_CSV_COLUMNS = ("order_no", "trade_type", "side", "signal", "date", "time",
                     "price", "contracts", "position_after")


def write_fills_csv(path, fills, tick=None):
    """Write the human-facing fill log: one row per fill, with ``trade_type``.

    ``price`` is rounded to the decimals of ``tick`` when given (display only;
    the digest hashes the unrounded fills).

    ``side`` is the hashed buy/sell field (``buy`` / ``sell``); ``trade_type``
    is from ``fill_trade_types``; ``position_after`` is the signed position.
    """
    position = 0
    dp = None if tick is None else tick_decimals(tick)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(FILLS_CSV_COLUMNS)
        for i, (fill, label) in enumerate(zip(fills, fill_trade_types(fills)), 1):
            qty = int(fill["contracts"])
            buy = fill["type"] == BUY_SIDE
            position += qty if buy else -qty
            writer.writerow([i, label, "buy" if buy else "sell", fill["signal"],
                             fill["date"], fill["time"], _fmt_price(fill["price"], dp),
                             qty, position])


def _fmt_price(price, dp):
    return repr(float(price)) if dp is None else f"{float(price):.{dp}f}"


def hashed_payload(fills, version=2):
    """The exact text ``fills_sha256`` hashes (and ``--dump`` writes).

    v2 starts with a ``digest_version: 2`` line; v1 is the bare fill lines.
    """
    head = "digest_version: 2\n" if version >= 2 else ""
    return head + "".join(canonical_fill_line(f) + "\n" for f in fills)


def fills_sha256(fills, version=2):
    """SHA-256 (utf-8) of ``hashed_payload(fills, version)``."""
    return hashlib.sha256(hashed_payload(fills, version).encode("utf-8")).hexdigest()


def count_round_turns(fills):
    """Round turns = exit records: each closes one previously entered contract."""
    return sum(1 for t in fill_trade_types(fills) if t.startswith("Exit"))


def format_digest(*, fills: int, round_turns: int, sha256: str, version: int = 2) -> str:
    """Canonical four-line digest text. Always ends with a newline."""
    return (
        f"powerbridge-trade-digest v{version}\n"
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
                       to_date, from_bar, seed_warmup, digest_version=2,
                       max_bars_back=0, max_entries_per_direction=0,
                       drawings=False):
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
    ``(None, reason)``. ``run_oracle`` prints a LOUD stderr note for a refusal
    once the Python run completes, so the caller never silently receives a
    partial or approximate trade list, and a program that stops on a user
    error reports only that error.
    """
    if (from_date is not None or to_date is not None or from_bar is not None
            or seed_warmup):
        return None, "run is windowed (not the native shape)"
    if digest_version < 2:
        return None, "--digest-v1 is served by the Python path only"
    try:
        objects = None
        if drawings:
            fills, objects = run_native_drawings(
                el_source, bars, config, data2_bars=data2_bars,
                max_bars_back=max_bars_back,
                max_entries_per_direction=max_entries_per_direction)
        else:
            fills = run_native(el_source, bars, config, data2_bars=data2_bars,
                               max_bars_back=max_bars_back,
                               max_entries_per_direction=max_entries_per_direction)
    except (AccelUnavailable, AccelRefused) as exc:
        return None, f"native path refused: {exc}"
    n = ohlcv_columns(bars)["n"]
    digest = {
        "fills": len(fills),
        "round_turns": count_round_turns(fills),
        # The FFI returns the ordered fill list, not the closed-position
        # ledger, so the native path cannot report closed_positions. It is not
        # part of the digest text.
        "closed_positions": None,
        "sha256": fills_sha256(fills),
        "n_bars": n,
        "n_warmup": 0,
        "records": fills,
        "native": True,
        "_native_drawings": objects,
    }
    digest["text"] = format_digest(
        fills=digest["fills"],
        round_turns=digest["round_turns"],
        sha256=digest["sha256"],
        version=digest_version,
    )
    return digest, None


def run_oracle(el_source: str, bars: Bars, config: InstrumentConfig, *,
               data2_closes: Optional[Sequence[float]] = None,
               data2_bars: Optional[Bars] = None,
               from_date: Optional[int] = None, to_date: Optional[int] = None,
               from_bar: Optional[int] = None, seed_warmup: bool = False,
               use_native: bool = False, digest_version: int = 2,
               max_bars_back: Union[int, str] = 0,
               max_entries_per_direction: Optional[int] = None,
               drawings: bool = False, drawing_case_id: Optional[str] = None,
               drawing_tape: Optional[dict[str, Any]] = None) -> dict[str, Any]:
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
    seed_warmup   : False (default): drop the prefix (tape slice).
                    True: keep the prefix, run it with fills
                    suppressed so Once / lookback / Variables stay primed.
                    A windowed run that disagrees on trades is a
                    bug, not a speed win.
    use_native    : False (default): the pure-Python path, byte-identical to
                    before. True: try the optional native kernel first (one FFI
                    crossing) for the full-tape shape; a Data2 program is
                    served by forwarding ``data2_bars``. If the kernel refuses
                    the program or is not built, fall back to the Python path
                    LOUDLY (a stderr note) — never a partial trade list. The
                    digest is identical whichever path runs.
    max_bars_back : skip the first N bars (after any window) as history only:
                    no evaluation, no orders, BarNumber starts at 1 on bar N+1
                    (MultiCharts MaxBarsBack). Default 0 evaluates every bar.
                    ``"auto"`` uses lookback.infer_max_bars_back(el_source)
                    (ValueError if it cannot be inferred).
    max_entries_per_direction : None/0 (default): every Buy/SellShort adds
                    to the position. N >= 1: an entry in the open position's
                    direction is refused while N entries are open (N=1 is
                    MultiCharts' documented default). Reversals always fill.
    drawings      : False (default). True records trendline, arrow and text
                    objects and adds ``drawings``, a powerbridge-drawing-dump
                    v1 dict. Requires ``drawing_case_id`` and ``drawing_tape``
                    with ``name`` and ``sha256`` (optional ``tick``). The sha
                    is the caller's tape identity (the golden corpus uses the
                    MultiCharts ASCII tape, not the PowerBridge CSV). Omit
                    both and the digest text is unchanged.
    drawing_case_id, drawing_tape : stored in that dump.

    Returns a dict with keys:
      text, fills, round_turns, closed_positions, sha256, n_bars, n_warmup,
      records, native, native_fallback_reason
      and, when ``drawings`` is true, ``drawings``.
    ``records`` is the get_mc_trades() list (ordered). ``round_turns`` is
    ``count_round_turns(records)`` (v1: ``len(records) // 2``).
    ``digest_version=1`` reproduces pre-v2 digests on the Python path.
    ``text`` is format_digest(). ``n_warmup`` is 0 unless seed_warmup ran a
    prefix. ``native`` is True iff the native kernel produced the digest;
    ``native_fallback_reason`` is a string when
    ``use_native`` was requested but the Python path ran, else ``None``. On
    the native path ``records`` are the kernel fills (no ``order_no``) and
    ``closed_positions`` is ``None`` (the FFI returns fills, not the
    closed-position ledger — neither is in the digest text).
    """
    max_bars_back = check_max_bars_back(max_bars_back, el_source)
    max_entries_per_direction = check_max_entries_per_direction(max_entries_per_direction)
    warn_short_history(el_source, max_bars_back, ohlcv_columns(bars)["n"], "pl_oracle")
    drawing_meta: tuple[Any, ...] = ()  # (case_id, tape name, sha256, tick) when drawings
    if drawings:
        from pl_transpiler.runtime._rt_drawing import (
            DrawingRecorder,
            make_dump,
            resolve_drawing_request,
        )
        from pl_transpiler.runtime.drawing_dump import build_dump
        drawing_meta = resolve_drawing_request(
            drawing_case_id, drawing_tape, config.tick)
    fallback_reason = None
    if use_native:
        native_digest, fallback_reason = _run_oracle_native(
            el_source, bars, config, data2_bars=data2_bars,
            from_date=from_date, to_date=to_date, from_bar=from_bar,
            seed_warmup=seed_warmup, digest_version=digest_version,
            max_bars_back=max_bars_back,
            max_entries_per_direction=max_entries_per_direction,
            drawings=drawings,
        )
        if native_digest is not None:
            objects = native_digest.pop("_native_drawings", None)
            if drawings:
                # The kernel recorded the objects; the envelope is built the
                # same way as on the Python path.
                cols = ohlcv_columns(bars)
                native_digest["drawings"] = build_dump(
                    objects or [],
                    case_id=drawing_meta[0], tape_name=drawing_meta[1],
                    tape_sha256=drawing_meta[2],
                    n_bars=len(cols["date"]),
                    first=(cols["date"][0], cols["time"][0]),
                    last=(cols["date"][-1], cols["time"][-1]),
                    tick=drawing_meta[3],
                    producer_kind="pb-native",
                )
            return native_digest

    if from_date is not None and from_bar is not None:
        raise ValueError("from_date and from_bar are mutually exclusive")
    from pl_transpiler.runtime.pl_runtime import (
        pl_unimplemented_reset,
        pl_unimplemented_warn,
    )
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
    ns: dict[str, Any] = {}
    exec(compile(py_code, "<oracle>", "exec"), ns)
    strategy_fn = ns["strategy"]

    cols = ohlcv_columns(bars)
    n = cols["n"]
    o_arr, h_arr, l_arr = cols["open"], cols["high"], cols["low"]
    c_arr, v_arr = cols["close"], cols["volume"]
    dates, times = cols["date"], cols["time"]

    fill_engine = FillEngine(config=config,
                             max_entries_per_direction=max_entries_per_direction)
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
    recorder = None
    if drawings:
        recorder = DrawingRecorder(dates, times, tick=drawing_meta[3])
    with open(os.devnull, "w", encoding="utf-8") as _devnull, \
            contextlib.redirect_stdout(_devnull), \
            (recorder if recorder is not None else contextlib.nullcontext()):
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
            max_bars_back=max_bars_back,
            on_bar=None if recorder is None else recorder.set_bar,
        )
    # Reported after the run, so a program that stops on a user error
    # prints only that error.
    if fallback_reason is not None and fallback_reason.startswith("native path refused"):
        print(
            f"[pl_oracle] {fallback_reason}; using the pure-Python engine (slower)",
            file=sys.stderr,
        )

    v1 = digest_version < 2
    records = fill_engine.get_mc_trades(v1=v1)
    # A round-turn is a matched entry+exit contract: one exit record each (v1
    # used fills//2). len(closed_trades) is closed *positions* (a ladder is one).
    digest = {
        "fills": len(records),
        "round_turns": len(records) // 2 if v1 else count_round_turns(records),
        "closed_positions": len(fill_engine.closed_trades),
        "sha256": fills_sha256(records, digest_version),
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
        version=digest_version,
    )
    if drawings:
        digest["drawings"] = make_dump(
            recorder, dates, times,
            case_id=drawing_meta[0], tape_name=drawing_meta[1],
            tape_sha256=drawing_meta[2], tick=drawing_meta[3],
            producer_kind="pb-python",
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
                    help="instrument config: a built-in name (NQ, ES) or a path to a "
                         "JSON file with the same fields, e.g. examples/configs/ES.json "
                         "(default: NQ).")
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
                         "dropping it. A disagreeing digest fails the check.")
    _cli.add_date_format_arg(ap)
    _cli.add_max_bars_back_arg(ap)
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="hide the warm-up note printed when a strategy reads "
                         "past bars and --max-bars-back is not set (other "
                         "warnings still print).")
    _cli.add_max_entries_per_direction_arg(ap)
    ap.add_argument("--data2", default=None,
                    help="aligned Data2 OHLCV CSV/.pbar; its close column drives "
                         "data2_close per bar on the Python path, and the full "
                         "tape is forwarded to the native kernel under --native.")
    ap.add_argument("--native", dest="use_native", action="store_true", default=None,
                    help="use the Rust kernel (the DEFAULT when it is installed); "
                         "falls back to Python loudly when it cannot serve the run.")
    ap.add_argument("--python", dest="use_native", action="store_false",
                    help="force the pure-Python path (reference / debugging).")
    ap.add_argument("--lenient", action="store_true",
                    help="treat EasyLanguage builtins the runtime does not implement "
                         "as 0/'' and warn, instead of stopping (same as PL_STRICT=0).")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write digest here as well as stdout.")
    ap.add_argument("--compare", default=None,
                    help="exit 1 if the produced digest differs from this file.")
    ap.add_argument("--dump", default=None,
                    help="write the hashed payload (version line + canonical fill lines) here. "
                         "Their first field is the buy/sell side, not a position label.")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v: also print the 'accel not installed' note and how many "
                         "bars skipped fill processing to stderr. -vv: also print "
                         "native-kernel internals ([accel] lines, e.g. the skip-region "
                         "plan); PB_DEBUG=1 does the same for those.")
    ap.add_argument("--drawings-json", default=None,
                    help="write a powerbridge-drawing-dump v1 file. Requires "
                         "--drawing-case. Does not change the digest on stdout.")
    ap.add_argument("--drawing-case", default=None,
                    help="case id stored in --drawings-json "
                         "(g-<family>-<NNN>-<slug>).")
    ap.add_argument("--tape-name", default=None,
                    help="tape name stored in the drawing dump "
                         "(default: the bars file stem).")
    ap.add_argument("--tape-sha", default=None,
                    help="64-hex sha256 of the tape the dump is compared "
                         "against (default: sha256 of the bars file bytes, "
                         "decompressed when the path ends in .gz).")
    ap.add_argument("--fills-csv", dest="fills_csv", default=None,
                    help="write a readable fill log CSV with a position-aware "
                         "trade_type column (EntryLong/ExitLong/EntryShort/ExitShort).")
    ap.add_argument("--digest-v1", dest="digest_v1", action="store_true",
                    help="emit the pre-v2 digest (for pinned v1 digests): no "
                         "reversal open legs or risk exits. Python path only.")
    ap.add_argument("--explain-native", dest="explain_native", action="store_true",
                    help="list every construct the native kernel would refuse in "
                         "EL_FILE, one 'kind name line' per line sorted by line, "
                         "then exit 0 without running anything (no bars CSV "
                         "needed). A compile-time check: a run can still be "
                         "refused at run time. Exits 2 if the native kernel is not installed.")
    _cli.add_version(ap, "pl_oracle")
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


def _file_sha256(path):
    """sha256 of a bars file. ``.gz`` is hashed after decompression."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if str(path).endswith(".gz"):
        raw = gzip.decompress(raw)
    return hashlib.sha256(raw).hexdigest()


def _drawing_tape_arg(args):
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


def _explain_native(el_file):
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


def _main(ap, args):
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
    note_warmup(el_source, args.max_bars_back, "pl_oracle", quiet=args.quiet,
                n_bars=ohlcv_columns(bars)["n"])
    warmup_note = (f" ({result['n_warmup']} warmup)" if result.get("n_warmup") else "")
    path_note = ("native" if result.get("native") else "python")
    print(f"[pl_oracle] {result['n_bars']} bars{warmup_note}, {result['fills']} fills, "
          f"{result['round_turns']} round-turns{mbb_note} [{path_note}]"
          + (f" -> {output}" if output else ""), file=sys.stderr)
    if args.verbose and result.get("fast_path_hits") is not None:
        print(f"[pl_oracle] {result['fast_path_hits']} bars had no pending orders, "
              "position or risk exits and skipped fill processing", file=sys.stderr)

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
