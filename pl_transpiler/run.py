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
pl_transpiler.run — General EL -> trades library (the ``pl_run`` CLI wraps it).

A THIN, reusable wrapper that runs ARBITRARY transpiled PowerLanguage/EasyLanguage
over SUPPLIED OHLCV bars + an instrument config and returns the per-bar trace
columns plus the orders/position/trades list — WITHOUT the capture-coupled
GT_FILES machinery in tests/run_gt.py.

It reuses the SAME bar-by-bar engine + FillEngine as the GT harness via the shared
`run_bar_loop` core (the shipped `pl_transpiler.engine` module, which the GT harness
re-exports), and the per-run instrument constants from the InstrumentConfig —
nothing is hard-coded and nothing is read from a capture. Unimplemented EL still
RAISES inside the runtime (fail-loud).

Library:
    from pl_transpiler import run_el, load_bars_csv, load_bars
    out = run_el(el_source, bars, config, trace_columns=[...])
    # out = {'columns': [...], 'rows': [[...], ...], 'trades': [...], 'n_rows': N}
    # Optional row_sink streams formatted rows during the loop and, by default,
    # does not retain them (avoids the GC tax of 2.32M list[str] rows). The CLI
    # uses that path. The default library call still returns list[list[str]].

CLI: ``pl_run`` (pl_transpiler.tools.pl_run).
"""
from __future__ import annotations

import contextlib
import csv
import itertools
import logging
import math
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Optional,
    Union,
)

if TYPE_CHECKING:
    from pl_transpiler.runtime.bar_tape import BarTape
    from pl_transpiler.runtime.instrument_config import InstrumentConfig

from pl_transpiler import transpile
from pl_transpiler._types import Node

# The bar-by-bar engine pieces (FillEngine, run_bar_loop, EL date/time parsers) ship in
# the package (pl_transpiler.engine / engine_dates), so the installed `pl_run` console script drives
# them directly from a bare wheel — no dependency on the private tests/ harness. The GT
# capture harness (tests/run_gt.py) re-exports the same objects, so behaviour is identical.
from pl_transpiler.engine import FillEngine, run_bar_loop
from pl_transpiler.engine_dates import infer_date_format, parse_el_date, parse_el_time
from pl_transpiler.errors import (
    UnimplementedKeywordError,
    el_runtime_error,
    format_error_lines,
)
from pl_transpiler.lexer import tokenise
from pl_transpiler.lookback import (
    check_max_bars_back,
    check_max_entries_per_direction,
    warn_short_history,
)
from pl_transpiler.parser import parse
from pl_transpiler.runtime._rt_core import (
    MissingBarColumnError,
    NamedExitUnverifiedError,
    UnimplementedBuiltinError,
)
from pl_transpiler.runtime.bar_tape import ohlcv_columns
from pl_transpiler.runtime.pl_runtime import reset_indicator_caches

# A bar series: a list of bar dicts (date, time, open, high, low, close, volume)
# or a column-contiguous BarTape holding the same values.
Bars = Union[Sequence[Mapping[str, Any]], "BarTape"]

# Kept under the old module name so existing log filters still match.
_log = logging.getLogger("pl_transpiler.tools.pl_run")


def _stop_matches(node: Node, exc: UnimplementedBuiltinError) -> bool:
    """True when AST ``node`` is the code that raised the strict-mode stop ``exc``."""
    if isinstance(exc, NamedExitUnverifiedError):
        entry = node.get('entry_label')
        return (node.get('type') == 'order'
                and node.get('action') == exc.verb.lower()
                and isinstance(entry, dict) and entry.get('value') == exc.entry)
    if isinstance(exc, MissingBarColumnError):
        return node.get('type') == 'position_kw' and node.get('kwarg') == exc.column
    name = node.get('name')
    return (node.get('type') in ('call', 'ident') and isinstance(name, str)
            and name.lower() == exc.builtin.lower())


def _stop_line(source: str, exc: UnimplementedBuiltinError) -> Optional[int]:
    """EL line of the first statement whose code raised ``exc``, or None.

    Walks the parsed program, so comments, strings and other statements that
    merely mention the word are skipped."""
    try:
        tokens, positions = tokenise(source)
        ast = parse(tokens, positions=positions, source=source)
    except Exception:  # noqa: BLE001 - fall back to the text search
        return None

    def walk(node: Any, line: Optional[int]) -> Optional[int]:
        if isinstance(node, list):
            for item in node:
                found = walk(item, line)
                if found:
                    return found
            return None
        if not isinstance(node, dict):
            return None
        line = node.get('_line') or line
        if _stop_matches(node, exc):
            return line
        for key, value in node.items():
            if key != '_comments' and isinstance(value, (dict, list)):
                found = walk(value, line)
                if found:
                    return found
        return None

    return walk(ast, None)


def unimplemented_stop_message(prog: str, exc: UnimplementedBuiltinError,
                               el_file: str) -> str:
    """One-line error for an unimplemented builtin that stopped a strict run,
    naming the source line of the statement that raised it (or, failing that,
    the first source line that mentions it)."""
    where = ""
    try:
        with open(el_file, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        source = None
    if source is not None:
        n = _stop_line(source, exc)
        if n is None:
            for i, line in enumerate(source.splitlines(), 1):
                if re.search(r"\b%s\b" % re.escape(exc.builtin), line, re.IGNORECASE):
                    n = i
                    break
        if n is not None:
            where = " (%s line %d)" % (os.path.basename(el_file), n)
    return "%s: error%s: %s" % (prog, where, exc)


def load_bars_csv(path: Union[str, os.PathLike[str]],
                  date_format: Optional[str] = None) -> list[dict[str, Any]]:
    """Load a plain OHLCV CSV into the bar list run_el expects.

    Header must contain: date,time,open,high,low,close,volume (case-insensitive).
    date/time accept either MultiCharts ASCII export form (D/M/YYYY, HH:MM[:SS]) or
    the EL integer form (YYYMMDD, HHMM); both are normalized to EL integers.
    ``date_format`` ('dmy', 'mdy' or 'ymd') sets the field order of slash dates.
    The default None infers it from the whole file (a first field above 12 is
    day-first, a second field above 12 month-first) and raises ValueError when
    the file cannot tell (see ``engine_dates.infer_date_format``). Invalid
    dates/times raise ValueError naming the line and value (see
    ``engine_dates.parse_el_date``). Bars must be oldest first: a bar older
    than the one before it raises ValueError naming the line.
    """
    if date_format is None:
        # 'dmy' is what None already reads, so only 'mdy' needs passing on.
        date_format = _infer_csv_date_format(path)
    bars = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        idx = _csv_header(reader)
        prev: Optional[tuple[int, int]] = None
        for row in reader:
            if not row:
                continue
            try:
                bar = _csv_bar(row, idx, date_format)
            except (ValueError, IndexError) as exc:
                raise ValueError("line %d: %s" % (reader.line_num, exc)) from exc
            # Bars must be oldest first. Equal timestamps are allowed: an
            # export at minute resolution can hold two bars stamped with the
            # same minute (tick or volume bars). Strictly decreasing is refused.
            stamp = (bar['date'], bar['time'])
            if prev is not None and stamp < prev:
                raise ValueError("bars out of order at line %d: %s comes after %s"
                                 % (reader.line_num, _fmt_stamp(stamp), _fmt_stamp(prev)))
            prev = stamp
            bars.append(bar)
    return bars


def _csv_header(reader: Any) -> dict[str, int]:
    """Read the header row -> column name -> index, or raise ValueError."""
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration as err:
        raise ValueError("empty file (want a header: %s)" % ",".join(_CSV_COLUMNS)) from err
    missing = [name for name in _CSV_COLUMNS if name not in header]
    if missing:
        raise ValueError("header is missing column(s) %s (want: %s)"
                         % (",".join(missing), ",".join(_CSV_COLUMNS)))
    return {name: header.index(name) for name in _CSV_COLUMNS}


def _infer_csv_date_format(path: Union[str, os.PathLike[str]]) -> Optional[str]:
    """``'mdy'`` when the file's slash dates are month-first, else None.

    A file whose first bar has an EL integer date is not scanned again."""
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        try:
            col = _csv_header(reader)['date']
        except ValueError:
            return None  # load_bars_csv reports it
        dates = (row[col].strip() for row in reader if len(row) > col)
        first = next(dates, '')
        if not re.search(r'[/-]', first):
            return None
        fmt = infer_date_format(itertools.chain((first,), dates))
    return 'mdy' if fmt == 'mdy' else None


def _fmt_stamp(stamp: tuple[int, int]) -> str:
    """EL (date, time) integers as ``YYYY-MM-DD HH:MM``."""
    d, t = int(stamp[0]), int(stamp[1])
    return "%04d-%02d-%02d %02d:%02d" % (d // 10000 + 1900, d // 100 % 100, d % 100,
                                         t // 100, t % 100)


_CSV_COLUMNS = ('date', 'time', 'open', 'high', 'low', 'close', 'volume')


def _csv_bar(row: list[str], idx: dict[str, int],
             date_format: Optional[str] = None) -> dict[str, Any]:
    return {
        'date': parse_el_date(row[idx['date']].strip(), date_format),
        'time': parse_el_time(row[idx['time']].strip()),
        'open': float(row[idx['open']]),
        'high': float(row[idx['high']]),
        'low': float(row[idx['low']]),
        'close': float(row[idx['close']]),
        'volume': float(row[idx['volume']]),
    }


def pbar_for(path: Union[str, os.PathLike[str]]) -> str:
    """Sibling ``.pbar`` path for a CSV path (same stem, ``.pbar`` extension)."""
    return os.path.splitext(str(path))[0] + ".pbar"


def load_bars(path: Union[str, os.PathLike[str]], prefer_pbar: bool = True,
              date_format: Optional[str] = None) -> Bars:
    """Load OHLCV from a CSV or a ``.pbar``.

    A ``.pbar`` is memory-mapped: opening one is ~0.006 s against
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
    return load_bars_csv(p, date_format)


def csv_to_pbar(csv_path: Union[str, os.PathLike[str]],
                pbar_path: Union[str, os.PathLike[str]],
                date_format: Optional[str] = None) -> Any:
    """Write a ``.pbar`` from an OHLCV CSV using the same parse as load_bars_csv."""
    from pl_transpiler.runtime.bar_tape import write_pbar
    return write_pbar(pbar_path, load_bars_csv(csv_path, date_format))


def _print_partial_manifest(errors: list[dict[str, Any]]) -> None:
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


def format_trace_cell(value: Any) -> str:
    """Format one trace value the way the GT harness writes a CSV cell.

    None (missing or explicit) becomes '0'; every other value is str(value).
    """
    return '0' if value is None else str(value)


def format_trace_row(trace: Mapping[str, Any], columns: Sequence[str]) -> list[str]:
    """Format one bar's trace dict into a list[str] aligned with columns."""
    get = trace.get
    return [format_trace_cell(get(k)) for k in columns]


# EasyLanguage spellings that do not reduce to a trace key by case/underscore
# folding alone (e.g. MarketPosition -> _market_position is handled by folding).
_COLUMN_ALIASES = {
    'marketpos': '_market_position',
    'currentbar': '_barnumber',
    'barnum': '_barnumber',
    'openpositionprofit': 'open_position_profit',
    'positionprofit': 'position_profit',
}


# EasyLanguage spelling of each built-in trace key, used when reporting
# column names; strategy variables are shown as declared.
_EL_COLUMN_NAMES = {
    '_barnumber': 'CurrentBar',
    '_close': 'Close',
    '_date': 'Date',
    '_time': 'Time',
    '_market_position': 'MarketPosition',
    'avgentryprice': 'AvgEntryPrice',
    'bars_since_entry': 'BarsSinceEntry',
    'bars_since_exit': 'BarsSinceExit',
    'current_contracts': 'CurrentContracts',
    'entry_price': 'EntryPrice',
    'exit_price': 'ExitPrice',
    'gross_loss': 'GrossLoss',
    'gross_profit': 'GrossProfit',
    'maxcontractsheld': 'MaxContractsHeld',
    'maxiddrawdown': 'MaxIDDrawdown',
    'net_profit': 'NetProfit',
    'open_position_profit': 'OpenPositionProfit',
    'openentriescount': 'OpenEntriesCount',
    'position_profit': 'PositionProfit',
    'totaltrades': 'TotalTrades',
}


def _el_name(key: str) -> str:
    return _EL_COLUMN_NAMES.get(key, key)


# Display rounding for the CLI's CSV only (run_el rows stay full precision).
# Money columns get cents; price columns get the instrument's tick decimals.
_MONEY_COLUMNS = frozenset((
    'netprofit', 'grossprofit', 'grossloss', 'openpositionprofit',
    'positionprofit', 'maxiddrawdown',
))
_PRICE_COLUMNS = frozenset(('entryprice', 'exitprice', 'avgentryprice', 'close'))


def tick_decimals(tick: float) -> int:
    """Decimal places of a tick size (0.25 -> 2, 1.0 -> 0), capped at 8."""
    text = f"{float(tick):.8f}".rstrip('0')
    return len(text.split('.')[1]) if '.' in text else 0


def display_row_sink(sink: Callable[[Iterable[Any]], Any], tick: float
                     ) -> tuple[Callable[[Iterable[Any]], None], Callable[[Iterable[Any]], None]]:
    """Wrap a (header, row) sink pair so money/price cells print rounded.

    Returns ``(header_sink, row_sink)``. Cells that are not plain floats
    (empty, ints, text) pass through unchanged.
    """
    price_dp = tick_decimals(tick)
    places: list[Optional[int]] = []

    def header(cols: Iterable[Any]) -> None:
        for c in cols:
            f = _fold(c)
            places.append(2 if f in _MONEY_COLUMNS
                          else price_dp if f in _PRICE_COLUMNS else None)
        sink(cols)

    def row(cells: Iterable[Any]) -> None:
        out = list(cells)
        for i, dp in enumerate(places):
            if dp is None or i >= len(out) or '.' not in str(out[i]):
                continue
            try:
                v = float(out[i])
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                out[i] = f"{v:.{dp}f}"
        sink(out)

    return header, row


class UnknownTraceColumnError(ValueError):
    """A requested trace column is not a key of the run's trace."""


def _fold(name: str) -> str:
    return name.lower().replace('_', '')


def resolve_trace_columns(requested: Iterable[str], available: Iterable[str]
                          ) -> list[str]:
    """Map requested column names onto trace keys, failing loud on unknowns.

    Exact keys win; otherwise names match case-insensitively and ignoring
    underscores (MarketPosition -> _market_position, NetProfit -> net_profit),
    then via a small EL alias table. Raises UnknownTraceColumnError (a ValueError) listing every unknown
    name with close-match suggestions.
    """
    import difflib
    available = list(available)
    exact = set(available)
    folded: dict[str, str] = {}
    for k in sorted(available, key=lambda k: (k.startswith('_'), k)):
        folded.setdefault(_fold(k), k)
    resolved: list[Any]
    unknown: list[str]
    resolved, unknown = [], []
    for name in requested:
        if name in exact:
            resolved.append(name)
            continue
        f = _fold(name)
        key = folded.get(f)
        if key is None and _COLUMN_ALIASES.get(f) in exact:
            key = _COLUMN_ALIASES[f]
        if key is None:
            unknown.append(name)
        resolved.append(key)
    if unknown:
        el_names = {_fold(_el_name(k)): _el_name(k) for k in available}
        parts = []
        for name in unknown:
            close = [el_names[m] for m in
                     difflib.get_close_matches(_fold(name), list(el_names), n=3, cutoff=0.6)]
            parts.append(f"{name!r}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        raise UnknownTraceColumnError(f"unknown trace column(s): {'; '.join(parts)}. "
                         f"Available: {', '.join(sorted(map(_el_name, available), key=str.lower))}")
    return resolved


def resolve_trade_from(bars: Bars, *, from_date: Optional[int] = None,
                       from_bar: Optional[int] = None) -> int:
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


def run_el(el_source: str, bars: Bars, config: InstrumentConfig, *,
           trace_columns: Optional[Sequence[str]] = None, partial: bool = False,
           row_sink: Optional[Callable[[list[str]], Any]] = None,
           header_sink: Optional[Callable[[list[str]], Any]] = None,
           retain_rows: Optional[bool] = None,
           from_date: Optional[int] = None, from_bar: Optional[int] = None,
           seed_warmup: bool = True, max_bars_back: Union[int, str, None] = None,
           max_entries_per_direction: Optional[int] = None,
           drawings: bool = False, drawing_case_id: Optional[str] = None,
           drawing_tape: Optional[dict[str, Any]] = None,
           use_native: bool = False) -> dict[str, Any]:
    """Transpile and run EL over supplied bars + instrument config.

    el_source     : EL/PowerLanguage source string.
    bars          : list of dicts with keys date,time,open,high,low,close,volume
                    (date as EL int CYYMMDD, time as EL int HHMM — see load_bars_csv)
                    or a BarTape (column-contiguous .pbar view; same values).
    config        : InstrumentConfig (the per-run config; use get_config('NQ')).
    trace_columns : optional list of trace keys to emit as output columns. When
                    None, the first bar's trace keys (sorted) define the columns.
                    Names match case-insensitively, ignoring underscores, plus
                    EL aliases (MarketPosition, NetProfit, CurrentBar, ...).
                    Unknown names raise ValueError on the first bar.
    partial       : opt-in partial mode. When True, unimplemented EL keywords
                    become execution-time stubs (raising only if evaluated) and the
                    stub manifest is printed LOUDLY to stderr before the run. The
                    default (partial=False) refuses to transpile them.
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
    max_bars_back : skip the first N bars as history only (MultiCharts
                    MaxBarsBack): no evaluation, no orders, no rows, and
                    BarNumber starts at 1 on bar N+1. Default None: the
                    lookback inferred from the source
                    (lookback.infer_max_bars_back), or 0 when it cannot be
                    inferred. 0 evaluates every bar. ``"auto"`` also infers,
                    but raises ValueError if it cannot. With too little
                    history for a ``Close[n]`` read, one warning is printed on
                    stderr; values are unchanged.
    max_entries_per_direction : None/0 (default): every Buy/SellShort adds
                    to the position. N >= 1: an entry in the open position's
                    direction is refused while N entries are open (N=1 is
                    MultiCharts' documented default). Reversals always fill.
    drawings      : False (default). True adds ``drawings``, a
                    powerbridge-drawing-dump v1 dict of the trendlines, arrows
                    and text the run created. Requires ``drawing_case_id`` and
                    ``drawing_tape`` ``{'name', 'sha256'}``. The default return
                    has no ``drawings`` key.
    use_native    : False (default): the Python engine. True: try the optional
                    native kernel's typed trace first; it serves full-tape runs
                    (max_bars_back and an entry cap included) without partial
                    mode or drawings, and its rows are byte-identical to the
                    Python rows.
                    Any other run, or a program the kernel refuses, runs on the
                    Python path and ``native_fallback_reason`` says why.

    Returns {'columns': [...], 'rows': [[str, ...], ...], 'trades': [...],
    'n_rows': int, 'n_warmup': int, 'native': bool,
    'native_fallback_reason': str | None, 'prints': [str, ...]}, where rows are
    formatted exactly as the GT
    harness formats predicted CSV cells and trades come from
    FillEngine.get_mc_trades(). When retain_rows is False, 'rows' is [] and
    n_rows is still the emit count. n_warmup is the seeded-prefix length (0
    when the tape is sliced or no window is set). 'prints' holds the
    strategy's ``Print`` lines in order (see pl_print for the format); the
    same lines on either engine.
    """
    return _run_el(el_source, bars, config, trace_columns=trace_columns,
                   partial=partial, row_sink=row_sink, header_sink=header_sink,
                   retain_rows=retain_rows, from_date=from_date,
                   from_bar=from_bar, seed_warmup=seed_warmup,
                   max_bars_back=max_bars_back,
                   max_entries_per_direction=max_entries_per_direction,
                   drawings=drawings, drawing_case_id=drawing_case_id,
                   drawing_tape=drawing_tape, use_native=use_native)


def _run_el(el_source: str, bars: Bars, config: InstrumentConfig, *,
            trace_columns: Optional[Sequence[str]] = None, partial: bool = False,
            row_sink: Optional[Callable[[list[str]], Any]] = None,
            header_sink: Optional[Callable[[list[str]], Any]] = None,
            retain_rows: Optional[bool] = None,
            from_date: Optional[int] = None, from_bar: Optional[int] = None,
            seed_warmup: bool = True, max_bars_back: Union[int, str, None] = None,
            max_entries_per_direction: Optional[int] = None,
            drawings: bool = False, drawing_case_id: Optional[str] = None,
            drawing_tape: Optional[dict[str, Any]] = None,
            use_native: bool = False,
            python_path_notify: Optional[Callable[..., Any]] = None,
            print_sink: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    """run_el, plus the CLI's hooks for the Python-path note (see
    _notify_python_path) and for each Print line (*print_sink*); not part of
    the public signature."""
    max_bars_back = check_max_bars_back(max_bars_back, el_source)
    max_entries_per_direction = check_max_entries_per_direction(max_entries_per_direction)
    fallback_reason = None
    if use_native:
        reason = _native_shape_refusal(
            partial=partial, from_date=from_date, from_bar=from_bar,
            drawings=drawings)
        if reason is None:
            out, fallback_reason = _run_el_native(
                el_source, bars, config, max_bars_back=max_bars_back,
                max_entries_per_direction=max_entries_per_direction,
                trace_columns=trace_columns,
                row_sink=row_sink, header_sink=header_sink, retain_rows=retain_rows)
            if out is not None:
                if print_sink is not None:
                    for line in out['prints']:
                        print_sink(line)
                return out
        else:
            fallback_reason = reason
    py_code = _transpile_for_run(el_source, partial)
    ns: dict[str, Any] = {}
    exec(compile(py_code, "<pl_run>", "exec"), ns)
    strategy_fn = ns['strategy']

    trade_from = 0
    if from_date is not None or from_bar is not None:
        idx = resolve_trade_from(bars, from_date=from_date, from_bar=from_bar)
        if seed_warmup:
            trade_from = idx
        else:
            bars = bars[idx:]

    cols = ohlcv_columns(bars)
    n = cols['n']
    dates, times = cols['date'], cols['time']

    # Trace keys come from the strategy's own Trace calls, so the per-bar
    # trade-stat/introspection keys stay off here.
    fill_engine = FillEngine(config=config,
                             max_entries_per_direction=max_entries_per_direction,
                             capture_stats=False)

    collector = _RowCollector(
        trace_columns, row_sink=row_sink, header_sink=header_sink,
        retain_rows=row_sink is None if retain_rows is None else retain_rows)

    from pl_transpiler.runtime._rt_core import _run_state
    from pl_transpiler.runtime.pl_runtime import pl_unimplemented_warn
    warn_short_history(el_source, max_bars_back, n, 'pl_run')
    drawing_meta: tuple[Any, ...] = ()  # (case_id, tape name, sha256, tick) when drawings
    recorder = None
    if drawings:
        from pl_transpiler.runtime._rt_drawing import (
            DrawingRecorder,
            resolve_drawing_request,
        )
        drawing_meta = resolve_drawing_request(
            drawing_case_id, drawing_tape, config.tick)
        recorder = DrawingRecorder(dates, times, tick=drawing_meta[3])
    # This run's own strictness and unimplemented-call counts.
    with _run_state() as run_st:
        prints = _run_strategy_loop(
            strategy_fn, py_code, el_source, '<pl_run>', cols, config, fill_engine,
            collector.emit_row, trade_from=trade_from, max_bars_back=max_bars_back,
            recorder=recorder, print_sink=print_sink)

    # Reported after the run, so a program that stops on a user error prints
    # only that error.
    _notify_python_path(python_path_notify, fallback_reason, use_native)
    out = {
        'columns': collector.final_columns(),
        'rows': collector.rows,
        'trades': fill_engine.get_mc_trades(),
        'n_rows': collector.n_emitted,
        # The seeded prefix; 0 when the tape is sliced or no window is set.
        'n_warmup': trade_from,
        # {keyword: calls} of unimplemented builtins this run used as 0/'' (warned on stderr).
        'unimplemented': pl_unimplemented_warn('pl_run', state=run_st),
        'native': False,
        'native_fallback_reason': fallback_reason,
        'prints': prints,
    }
    if drawings:
        from pl_transpiler.runtime._rt_drawing import make_dump
        out['drawings'] = make_dump(
            recorder, dates, times,
            case_id=drawing_meta[0], tape_name=drawing_meta[1],
            tape_sha256=drawing_meta[2], tick=drawing_meta[3],
            producer_kind='pb-python',
        )
    return out


def _run_strategy_loop(strategy_fn: Callable[..., Any], py_code: str, el_source: str,
                       filename: str, cols: Mapping[str, Any], config: InstrumentConfig,
                       fill_engine: FillEngine,
                       emit_row: Callable[[int, int, dict[str, Any]], Any], *,
                       trade_from: int, max_bars_back: int, recorder: Any,
                       per_bar_extra: Optional[Callable[[int], dict[str, Any]]] = None,
                       emit_trace: bool = True,
                       print_sink: Optional[Callable[[str], Any]] = None) -> list[str]:
    """Run the strict bar loop over *cols* (``ohlcv_columns``) for run_el / run_oracle.

    Fail-loud: a general entrypoint has no throw-sidecar gate, so any EL surface
    the runtime cannot reproduce must RAISE (strict) rather than be silently
    counted. The strategy's ``Print`` lines are returned (and passed to
    *print_sink* as they are printed); anything else written to stdout is
    discarded so it can never corrupt the CLI's stdout. A runtime error is
    re-raised against the EL source line when it can be mapped. *recorder* (or
    None) is the drawing recorder, entered for the run.
    """
    from pl_transpiler.runtime._rt_datetime import _capture_prints
    o_arr, h_arr, l_arr = cols['open'], cols['high'], cols['low']
    c_arr, v_arr = cols['close'], cols['volume']

    def get_ohlcv(i: int) -> tuple[Any, Any, Any, Any, Any]:
        return (o_arr[i], h_arr[i], l_arr[i], c_arr[i], v_arr[i])

    with open(os.devnull, 'w', encoding='utf-8') as _devnull, \
            contextlib.redirect_stdout(_devnull), \
            _capture_prints(print_sink) as prints, \
            (recorder if recorder is not None else contextlib.nullcontext()):
        try:
            run_bar_loop(
                strategy_fn,
                n_bars=cols['n'],
                get_ohlcv=get_ohlcv,
                dates=cols['date'],
                times=cols['time'],
                config=config,
                fill_engine=fill_engine,
                emit_row=emit_row,
                per_bar_extra=per_bar_extra,
                strict=True,
                trade_from=trade_from,
                emit_trace=emit_trace,
                max_bars_back=max_bars_back,
                on_bar=None if recorder is None else recorder.set_bar,
            )
        except (UnimplementedBuiltinError, UnimplementedKeywordError):
            raise
        except Exception as exc:
            el_exc = el_runtime_error(exc, py_code, filename, el_source)
            if el_exc is None:
                raise
            raise el_exc from exc
    return prints


def _transpile_for_run(el_source: str, partial: bool) -> str:
    """Trace-mode Python for ``run_el``; partial mode prints the stub manifest first."""
    reset_indicator_caches()
    if not partial:
        return transpile(el_source, trace=True)
    # Surface the manifest loudly before running. Detect the stubs via the
    # strict transpile's collect-all diagnostics (empty when nothing is stubbed).
    try:
        transpile(el_source, trace=True)
    except UnimplementedKeywordError as e:
        _print_partial_manifest(e.errors)
    return transpile(el_source, trace=True, partial=True)


class _RowCollector:
    """``run_el``'s ``emit_row``: fixes the columns on the first bar, then
    formats each bar's trace into a row for the sink and/or the kept rows."""

    def __init__(self, trace_columns: Optional[Sequence[str]], *,
                 row_sink: Optional[Callable[[list[str]], Any]],
                 header_sink: Optional[Callable[[list[str]], Any]],
                 retain_rows: bool) -> None:
        self.trace_columns = trace_columns
        self.row_sink = row_sink
        self.header_sink = header_sink
        self.retain_rows = retain_rows
        self.rows: list[Any] = []
        self.columns: Optional[list[str]] = None
        self.keys: Optional[list[str]] = None
        self.n_emitted = 0

    def emit_row(self, i: int, barnumber: int, trace: dict[str, Any]) -> None:
        keys = self.keys
        if keys is None:
            if self.trace_columns is not None:
                cols = list(self.trace_columns)
                keys = resolve_trace_columns(cols, trace.keys())
            else:
                cols = keys = sorted(trace.keys())
            self.columns = cols
            self.keys = keys
            if self.header_sink is not None:
                self.header_sink(cols)
        row = format_trace_row(trace, keys)
        self.n_emitted += 1
        if self.retain_rows:
            self.rows.append(row)
        if self.row_sink is not None:
            self.row_sink(row)

    def final_columns(self) -> list[str]:
        """The column list; with no bar emitted, the requested columns (sent
        to the header sink now) or []."""
        if self.columns is not None:
            return self.columns
        columns = list(self.trace_columns) if self.trace_columns is not None else []
        if self.header_sink is not None:
            self.header_sink(columns)
        return columns


def _notify_python_path(python_path_notify: Optional[Callable[..., Any]],
                        fallback_reason: Optional[str], use_native: bool) -> None:
    """Report that ``run_el`` ran on the Python path, and why.

    The library logs the note; the CLI passes warn_python_path to print it on
    stderr instead.
    """
    from pl_transpiler.runtime.accel import accel_available, python_path_note
    notify = python_path_notify or (lambda r=None: _log.info(python_path_note(r)))
    if fallback_reason is not None:
        notify(fallback_reason)
    elif not use_native and accel_available():
        notify("use_native=False (pl_run --native is the default when it is installed)")
    elif not use_native:
        notify()


def _native_shape_refusal(*, partial: bool, from_date: Optional[int],
                          from_bar: Optional[int], drawings: bool) -> Optional[str]:
    """Why ``run_el(use_native=True)`` must take the Python path before asking
    the kernel, or None. The typed trace serves the full-tape run only."""
    if partial:
        return "--partial is served by the Python path only"
    if from_date is not None or from_bar is not None:
        return "run is windowed (not the native shape)"
    if drawings:
        return "drawings are served by the Python path only"
    return None


def _run_el_native(el_source: str, bars: Bars, config: InstrumentConfig, *,
                   max_bars_back: int,
                   max_entries_per_direction: Optional[int],
                   trace_columns: Optional[Sequence[str]],
                   row_sink: Optional[Callable[[list[str]], Any]],
                   header_sink: Optional[Callable[[list[str]], Any]],
                   retain_rows: Optional[bool]
                   ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The native typed trace, rendered the way ``emit_row`` renders it.

    Returns ``(out, None)`` (``run_el``'s dict) or ``(None, reason)`` when the
    kernel is missing or refuses the program. Nothing reaches the sinks before
    the kernel has produced the whole trace, so a refusal never leaves a
    partial CSV behind.
    """
    from pl_transpiler.runtime.accel import (
        AccelRefused,
        AccelUnavailable,
        run_native_trace,
    )
    try:
        prints: list[str] = []
        native_cols, native_rows, fills = run_native_trace(
            el_source, bars, config, max_bars_back=max_bars_back,
            max_entries_per_direction=max_entries_per_direction, prints=prints)
    except AccelUnavailable as exc:
        return None, str(exc)
    except AccelRefused as exc:
        return None, f"native path refused: {exc}"
    warn_short_history(el_source, max_bars_back, len(bars), 'pl_run')
    if retain_rows is None:
        retain_rows = row_sink is None
    columns = list(trace_columns) if trace_columns is not None else []
    rows = []
    if native_rows:
        # As emit_row: the first bar's keys define (or resolve) the columns;
        # an unknown name raises only when there is a bar to emit.
        if trace_columns is not None:
            keys = resolve_trace_columns(columns, native_cols)
        else:
            columns = keys = list(native_cols)
        index = {k: i for i, k in enumerate(native_cols)}
        picks = [index[k] for k in keys]
        if header_sink is not None:
            header_sink(columns)
        for typed in native_rows:
            row = [format_trace_cell(typed[i]) for i in picks]
            if retain_rows:
                rows.append(row)
            if row_sink is not None:
                row_sink(row)
    elif header_sink is not None:
        header_sink(columns)
    return {
        'columns': columns,
        'rows': rows,
        # Same shape as FillEngine.get_mc_trades(): order_no first, from 1.
        'trades': [{'order_no': i + 1, **fill} for i, fill in enumerate(fills)],
        'n_rows': len(native_rows),
        'n_warmup': 0,
        'unimplemented': {},
        'native': True,
        'native_fallback_reason': None,
        'prints': prints,
    }, None
