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

"""bar_loop.py -- the per-bar execution core (:func:`run_bar_loop`)."""
import datetime as _dt
import os
import traceback
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

from pl_transpiler.engine_dates import el_date_to_day_of_week
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError
from pl_transpiler.runtime._rt_history import SeriesState

if TYPE_CHECKING:
    from pl_transpiler.engine import FillEngine

# FillEngine.fill_pos_kwargs keys → attribute names. Persist 8-arg path
# writes only the keys the strategy names; **kwargs still writes the full set.
_POS_KWARGS: tuple[tuple[str, str], ...] = (
    ('market_position', 'market_position'),
    ('current_contracts', 'contracts'),
    ('entry_price', 'entry_price'),
    ('entry_date', 'entry_date'),
    ('entry_time', 'entry_time'),
    ('avgentryprice', 'avgentryprice'),
    ('bars_since_entry', 'bars_since_entry'),
    ('exit_price', 'exit_price'),
    ('bars_since_exit', 'bars_since_exit'),
    ('open_position_profit', 'open_position_profit'),
    ('position_profit', 'position_profit'),
    ('net_profit', 'net_profit'),
    ('gross_profit', 'gross_profit'),
    ('gross_loss', 'gross_loss'),
    ('totaltrades', 'total_trades'),
    ('maxcontractsheld', 'max_contracts_held'),
    ('maxiddrawdown', 'max_id_drawdown'),
    ('maxshares', 'max_shares'),
    ('openentriescount', 'open_entries_count'),
)
# Closed-trade performance words (NumWinTrades, PercentProfit, ...) → engine
# attributes. Updated at trade close, like totaltrades / gross_profit.
_PERF_KWARGS = (
    ('numwintrades', '_st_nwin'),
    ('numlostrades', '_st_nlos'),
    ('numeventrades', '_st_neven'),
    ('percentprofit', 'pct_profit'),
    ('largestwintrade', '_st_lrgwin'),
    ('largestlostrade', '_st_lrglos'),
    ('maxconsecwinners', '_st_maxcwin'),
    ('maxconseclosers', '_st_maxclos'),
    ('totalbarswintrades', '_st_totbwin'),
    ('totalbarslostrades', '_st_totblos'),
    ('totalbarseventrades', '_st_totbeven'),
    ('avgbarswintrade', 'avg_bars_win'),
    ('avgbarslostrade', 'avg_bars_los'),
    ('avgbarseventrade', 'avg_bars_even'),
)
_POS_KWARGS = _POS_KWARGS + _PERF_KWARGS
_DAILY_OHLC_KEYS = ('opend_0', 'highd_0', 'lowd_0', 'closed_0', 'closed_1')


def _code_has_const(fn: Any, name: str) -> bool:
    """True if *name* is a string constant in *fn* or a nested code object.

    Generated persist/trace reads kwargs via ``kwargs.get('key', default)``, so
    the key is a ``co_consts`` string. Fail closed (True) when *fn* cannot be
    inspected — compute the value rather than skip it.
    """
    code = getattr(fn, "__code__", None)
    if code is None:
        return True
    stack = [code]
    seen: set[Any] = set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        consts = c.co_consts
        if name in consts:
            return True
        for x in consts:
            if hasattr(x, "co_consts"):
                stack.append(x)
    return False


_MIRROR_PARAMS = ("open", "high", "low", "close", "volume", "date", "time")


def _is_mirror_dialect(strategy_fn: Any, code: Any) -> bool:
    """True for a strategy emitted by transpile(..., dialect='mirror'): the
    generated module marker plus `(open, ..., time, **kwargs)` with no `_state`."""
    if code is None or not code.co_flags & 0x08:  # CO_VARKEYWORDS
        return False
    glb = getattr(strategy_fn, "__globals__", {})
    return ("__builtin_len" in glb
            and code.co_argcount == 7
            and code.co_varnames[:8] == _MIRROR_PARAMS + ("kwargs",)
            and "_state" not in code.co_varnames)


def _validate_window(trade_from: int, max_bars_back: Union[int, str],
                     n_bars: int) -> tuple[int, int]:
    """Normalise and range-check ``trade_from`` and ``max_bars_back``."""
    trade_from = int(trade_from or 0)
    if trade_from < 0:
        raise ValueError(f"trade_from must be >= 0, got {trade_from}")
    if trade_from > n_bars:
        raise ValueError(f"trade_from {trade_from} exceeds n_bars {n_bars}")
    max_bars_back = int(max_bars_back or 0)
    if max_bars_back < 0:
        raise ValueError(f"max_bars_back must be >= 0, got {max_bars_back}")
    return trade_from, max_bars_back


def _takes_kwargs_dict(strategy_fn: Any) -> bool:
    """True when *strategy_fn* takes the kwargs dict as its 8th positional.

    Raises ValueError for mirror-dialect output, which has no cross-bar state.
    """
    # Persist/trace generated strategy() takes the kwargs dict as the 8th
    # positional (`kwargs=None, **_kw`). Passing call_kwargs by reference
    # skips a ~40-key **unpack copy every bar. Hand-written `**kwargs`
    # strategies (tests, custom runners) keep the copy path.
    _fn_code = getattr(strategy_fn, "__code__", None)
    _pass_kw_dict = (
        _fn_code is not None
        and _fn_code.co_argcount >= 8
        and _fn_code.co_varnames[7] == "kwargs"
    )
    if _is_mirror_dialect(strategy_fn, _fn_code):
        raise ValueError(
            "strategy is mirror-dialect transpiler output (no cross-bar _state, "
            "so Vars reset every bar); transpile it with the default "
            "dialect='runnable' instead of dialect='mirror' / --no-persist")
    return _pass_kw_dict


@dataclass(frozen=True)
class _KwargNeeds:
    """Which per-bar kwargs the strategy reads (all True for ``**kwargs``)."""

    datetime: bool = True
    daily: bool = True
    session_last: bool = True
    currentsession: bool = True
    all_pos: bool = True
    mp: bool = False
    cc: bool = False
    pos_extra: tuple[tuple[str, str], ...] = ()
    current_date: bool = True
    current_time: bool = True
    currentbar: bool = True
    barnumber: bool = True
    outpath: bool = True
    first_bar: bool = True


def _kwarg_needs(strategy_fn: Any, pass_kw_dict: bool) -> _KwargNeeds:
    """Decide which per-bar kwargs to compute for *strategy_fn*."""
    # Persist/trace 8-arg path: skip unused kwargs work when the generated
    # body never names the key. **kwargs callers (tests, dumps) always compute.
    if not pass_kw_dict:
        return _KwargNeeds()
    _pos_needed = tuple(
        (k, a) for k, a in _POS_KWARGS
        if _code_has_const(strategy_fn, k)
    )
    _need_all_pos = len(_pos_needed) == len(_POS_KWARGS)
    return _KwargNeeds(
        datetime=_code_has_const(strategy_fn, "datetime"),
        daily=any(_code_has_const(strategy_fn, k) for k in _DAILY_OHLC_KEYS),
        session_last=_code_has_const(strategy_fn, "session_last_bar"),
        currentsession=_code_has_const(strategy_fn, "currentsession"),
        all_pos=_need_all_pos,
        mp=(not _need_all_pos) and any(
            k == "market_position" for k, _a in _pos_needed),
        cc=(not _need_all_pos) and any(
            k == "current_contracts" for k, _a in _pos_needed),
        pos_extra=tuple(
            p for p in _pos_needed
            if p[0] not in ("market_position", "current_contracts")
        ),
        current_date=_code_has_const(strategy_fn, "current_date"),
        current_time=_code_has_const(strategy_fn, "current_time"),
        currentbar=_code_has_const(strategy_fn, "currentbar"),
        barnumber=_code_has_const(strategy_fn, "barnumber"),
        outpath=_code_has_const(strategy_fn, "outpath"),
        first_bar=_code_has_const(strategy_fn, "_first_bar"),
    )


_History = tuple[list[Any], list[Any], list[Any], list[Any],
                 list[Any], list[Any], list[Any]]


def _init_history(init_hist: Optional[dict[str, list[Any]]]) -> _History:
    """The growing o/h/l/c/v/d/t history lists the strategy reads."""
    # Growing-history buffers. PAD=1 leading zero mirrors run_gt; init_hist lets a
    # caller pre-seed real pre-capture warmup bars (GTA5 Data1).
    # These stay Python lists. Isolated FAST-LANE (856,914 bars):
    # array.array append+[-1] is ~1.2× slower (re-boxes a float on every read);
    # a Python __getitem__ wrapper is ~2.7× slower; MaxBarsBack pop(0) cap is
    # noise on the reference strategy. strategy() wants Python floats; list.append stores the
    # tape's existing PyFloat. A contiguous buffer is the right FFI layout for
    # the optional accel extra — do not silently switch this path.
    if init_hist is not None:
        return (init_hist['o'], init_hist['h'], init_hist['l'], init_hist['c'],
                init_hist['v'], init_hist['d'], init_hist['t'])
    PAD = 1
    o_hist, h_hist, l_hist, c_hist = ([0.0] * PAD for _ in range(4))
    v_hist, d_hist, t_hist = ([0] * PAD for _ in range(3))
    return o_hist, h_hist, l_hist, c_hist, v_hist, d_hist, t_hist


def _infer_session_end_tm(dates: Any, times: Any, n_bars: int) -> Optional[int]:
    """Most common time-of-day at a day boundary, or None if there is none.

    Classifies the FINAL bar's SessionLastBar (no next bar to compare).
    Derived from the date/time series so it stays instrument-agnostic.
    """
    if n_bars <= 1:
        return None
    _se: Counter[Any] = Counter()
    for _j in range(n_bars - 1):
        if dates[_j] != dates[_j + 1]:
            _se[times[_j]] += 1
    if _se:
        return _se.most_common(1)[0][0]  # type: ignore[no-any-return]
    return None


def _lastcalc_kwargs(dates: Any, times: Any, n_bars: int) -> dict[str, Any]:
    """LastCalc* keys: datetime of the FINAL bar (constant across bars)."""
    if n_bars <= 0:
        return {}
    try:
        _ld = int(dates[n_bars - 1]); _lt = int(times[n_bars - 1])
        _yr = 1900 + _ld // 10000; _mo = (_ld // 100) % 100; _dy = _ld % 100
        _hh = _lt // 100; _mn = _lt % 100
        _jdate = (_dt.date(_yr, _mo, _dy) - _dt.date(1899, 12, 30)).days
        _secs = _hh * 3600 + _mn * 60
        return {
            'lastcalcjdate': _jdate,
            'lastcalcdatetime': _jdate + _secs / 86400.0,
            'lastcalcmmtime': _hh * 60 + _mn,
            'lastcalcsstime': _secs,
            'lastcalcmstime': _secs * 1000,
        }
    except (ValueError, TypeError):
        return {}


def _const_defaults(config: Any, lastcalc_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Per-run constant kwargs, restored after ``per_bar_extra`` overrides them."""
    # Constants never change unless extra overrides them. Restore only those
    # keys next bar (old path re-wrote all 10 + lastcalc every bar).
    defaults = {
        'sess1starttime': config.sess1starttime,
        'barinterval': config.barinterval,
        'bartype': 1,
        'tradedate': 101,
        'pointvalue': config.pointvalue,
        'bigpointvalue': config.bigpointvalue,
        'minmove': config.minmove,
        'pricescale': config.pricescale,
        'symbol': config.symalt,
        'symbolname': config.symnm,
        'description': config.descstr,
        'category': config.category,
        'symbolcurrencycode': config.symccy,
        'expirationdate': config.expdate,
    }
    if lastcalc_kwargs:
        defaults.update(lastcalc_kwargs)
    return defaults


def _initial_call_kwargs(config: Any, lastcalc_kwargs: dict[str, Any],
                         state: SeriesState,
                         closed_trades: list[Any]) -> dict[str, Any]:
    """The single kwargs dict reused (overwritten in place) on every bar."""
    # One kwargs dict for the whole run. Per-bar keys, constants, and position
    # fields are overwritten in place (no new dict, one **-unpack). A fresh
    # dict + double **-unpack every bar was ~1.5 s isolated over 2.32M dummy calls.
    call_kwargs = {
        # OutPath is blanked so a strategy's Print(File(OutPath)) never writes to
        # the path in its source (sandbox). Other input names are not touched.
        'outpath': '',
        'sess1starttime': config.sess1starttime,
        'barinterval': config.barinterval,
        'bartype': 1,
        'tradedate': 101,
        'pointvalue': config.pointvalue,
        'bigpointvalue': config.bigpointvalue,
        'minmove': config.minmove,
        'pricescale': config.pricescale,
        'symbol': config.symalt,
        'symbolname': config.symnm,
        'description': config.descstr,
        'category': config.category,
        'symbolcurrencycode': config.symccy,
        'expirationdate': config.expdate,
        'current_date': 0,
        'current_time': 0,
        'datetime': 0.0,
        'opend_0': 0.0,
        'highd_0': 0.0,
        'lowd_0': 0.0,
        'closed_0': 0.0,
        'closed_1': -1,
        'currentsession': 2,  # dummy; recomputed on the first bar
        'currentbar': 0,
        'session_last_bar': 0,
        'barnumber': 0,
        '_first_bar': True,
        '_state': state,
        # Closed-position ledger for EntryDate(n)/ExitPrice(n)/... (pl_pos_ago).
        # The engine appends to this list in place, so one reference suffices.
        '_closed_trades': closed_trades,
        'time_s': 0,
        'market_position': 0,
        'current_contracts': 0,
        'entry_price': 0,
        'avgentryprice': 0,
        'bars_since_entry': 0,
        'exit_price': 0,
        'bars_since_exit': 0,
        'open_position_profit': 0,
        'position_profit': 0,
        'net_profit': 0,
        'gross_profit': 0,
        'gross_loss': 0,
        'totaltrades': 0,
        'maxcontractsheld': 0,
        'maxiddrawdown': 0,
        'maxshares': 0,
        'openentriescount': 0,
    }
    if lastcalc_kwargs:
        call_kwargs.update(lastcalc_kwargs)
    return call_kwargs


def _exit_on_close(fill_engine: "FillEngine", prev_close: Any, barnumber: int,
                   prev_dt: Any, prev_tm: Any) -> None:
    """SetExitOnClose at a date change: flatten at the previous bar's close."""
    # A 'this bar on close' order placed on the prior (last session) bar fills
    # FIRST at that close — it may REVERSE the position — and THEN exit-on-close
    # flattens whatever remains. MC counts BOTH (verified o3 bn66595->66596:
    # SE_CLS reverses long->short (+1 trade), exit-on-close closes the short
    # (+1), session ends flat with tot 2526->2528). Both fills price at the
    # last bar's close. _execute_onclose_pending clears its own queue, so the
    # subsequent before_bar call is a no-op.
    fill_engine._execute_onclose_pending()
    if fill_engine.market_position != 0:
        fill_engine._close_at_stop(prev_close, 'exit_on_close', barnumber - 1,
                                   exit_date=prev_dt, exit_time=prev_tm,
                                   exit_name='End of Day Exit', exit_cat=4)


class _DailyOHLC:
    """Running day OHLC for OpenD/HighD/LowD/CloseD and CurrentSession.

    Updated once per bar (history-only bars too) when the strategy reads the
    daily or CurrentSession kwargs.
    """

    __slots__ = ('open', 'high', 'low', 'close', 'prev_close', 'session')

    def __init__(self) -> None:
        self.open: Any = 0.0
        self.high: Any = 0.0
        self.low: Any = 0.0
        self.close: Any = 0.0
        self.prev_close: Any = -1  # CloseD(1): yesterday's close; -1 when no prior day
        self.session = 2  # dummy; recomputed on the first bar

    def update(self, prev_date: Any, dt: Any, o: Any, h: Any, l: Any, c: Any) -> None:
        """Fold one bar in; *prev_date* is None on the first bar."""
        if prev_date is None:
            self.open = o
            self.high = h
            self.low = l
            self.prev_close = -1
            self.session = el_date_to_day_of_week(dt)
        elif dt != prev_date:
            self.prev_close = self.close
            self.session = el_date_to_day_of_week(dt)
            self.open = o
            self.high = h
            self.low = l
        else:
            self.high = max(self.high, h)
            self.low = min(self.low, l)
        self.close = c


def _el_datetime(dt: Any, tm: Any) -> Any:
    """DateTime (YYYYMMDD.frac) for DateTime-decomposition functions."""
    yyyy = 1900 + (dt // 10000)
    mm = (dt // 100) % 100
    dd = dt % 100
    date_part = yyyy * 10000 + mm * 100 + dd
    hh = tm // 100
    mm_t = tm % 100
    return date_part + (hh * 3600 + mm_t * 60) / 86400.0


def _session_last_bar(dates: Any, i: int, n_bars: int, dt: Any, tm: Any,
                      session_end_tm: Optional[int]) -> int:
    """SessionLastBar: 1 when the next bar belongs to a new day.

    The final bar has no successor, so it is classified by time-of-day.
    """
    if i + 1 < n_bars:
        return 1 if dates[i + 1] != dt else 0
    return 1 if (session_end_tm is not None and tm == session_end_tm) else 0


def _apply_bar_extra(call_kwargs: dict[str, Any], extra: Optional[dict[str, Any]],
                     engine_keys: frozenset[str], const_defaults: dict[str, Any],
                     hook_only: list[str],
                     restore_consts: tuple[str, ...]) -> tuple[str, ...]:
    """Merge this bar's ``per_bar_extra`` dict; return the consts it overrode.

    First undo the previous bar's extra: restore only the const keys it
    overrode (so an omitted extra still matches a rebuilt dict; generated
    persist never pops, ``**kwargs`` pops a copy and cannot hole
    *call_kwargs*), and drop its hook-only keys so a value from a capture
    column missing on this bar does not carry over. Then merge *extra*
    (tick fields, Data2, Len/Prec); generic values already set win for keys
    the hook does not provide.
    """
    for k in restore_consts:
        call_kwargs[k] = const_defaults[k]
    if hook_only:
        for k in hook_only:
            call_kwargs.pop(k, None)
        hook_only.clear()
    if not extra:
        return ()
    call_kwargs.update(extra)
    for k in extra:
        if k not in engine_keys:
            hook_only.append(k)
    return tuple(k for k in extra if k in const_defaults)


def run_bar_loop(
    strategy_fn: Callable[..., Any],
    *,
    n_bars: int,
    get_ohlcv: Callable[[int], Any],
    dates: Any,
    times: Any,
    config: Any,
    fill_engine: "FillEngine",
    emit_row: Callable[[int, int, dict[str, Any]], Any],
    warmup: int = 0,
    init_hist: Optional[dict[str, list[Any]]] = None,
    per_bar_extra: Optional[Callable[[int], Optional[dict[str, Any]]]] = None,
    lastcalc_kwargs: Optional[dict[str, Any]] = None,
    session_end_tm: Optional[int] = None,
    strict: Optional[bool] = None,
    trade_from: int = 0,
    emit_trace: bool = True,
    max_bars_back: Union[int, str] = 0,
    on_bar: Optional[Callable[[int], Any]] = None,
) -> tuple[int, int, Optional[str]]:
    """Shared per-bar execution core used by BOTH run_gt (the GT capture harness)
    and the general entrypoint (pl_transpiler.run.run_el).

    Given a transpiled trace-mode strategy_fn, per-bar OHLCV (via get_ohlcv) and the
    EL date/time series, this drives the SAME engine sequence run_gt always used:
    before_bar (pending fills + MM stops + PnL) -> assemble the per-bar
    bar/contract/session kwargs from the per-run InstrumentConfig (NOT hard-coded)
    -> call the strategy -> after_bar (queue next-bar orders). Capture-specific
    extras (tick fields, Data2, Len/Prec) are supplied by the optional
    per_bar_extra(i) hook; everything generic (daily OHLC, session, LastCalc*,
    SessionLastBar, contract spec) is computed here so the two callers cannot
    diverge. Returns (throw_count, total_strategy_calls, first_traceback).

    Generated persist/trace strategy() takes the kwargs dict as the 8th
    positional argument so this loop can pass ``call_kwargs`` by reference
    (no per-bar ``**`` copy). Hand-written ``def strategy(..., **kwargs)``
    callers still receive a copied unpack.

    trade_from: first tape index that may fill or emit (seeded warmup).
    Bars before it still run the strategy, grow history, and persist `_state`
    (Once / Average / Close[1] / Variables see the prefix), but FillEngine fills
    are suppressed. The last warmup bar may queue "next bar" orders so a signal
    on the bar before the window still fills on the first trading bar. Default 0
    runs every bar normally. Independent of `warmup`, which only
    skips output rows (GT capture alignment) and still processes fills.

    emit_trace: when True (default), FillEngine.inject_trace writes position
    columns into the per-bar trace dict before emit_row — required for run_el /
    GT CSV output. persist min-emit (pl_oracle, scoring A/B) does not read that
    dict; pass False to skip the 14 unused stores/bar. Fills and `_state` are
    unchanged. Default True is the previous loop.

    max_bars_back: the first N tape bars are history only (MultiCharts
    MaxBarsBack): prices are appended so lookbacks see them, but the strategy
    does not run, no order is placed or filled, no row is emitted, and
    BarNumber starts at 1 on bar N+1. Daily OHLC tracking still sees them.
    Default 0 evaluates every bar (the previous loop).

    on_bar: optional callable(i) invoked with the 0-based tape index immediately
    before the strategy runs on that bar (not on MaxBarsBack history-only
    bars). ``None`` (default) changes nothing. Drawing capture uses it to
    stamp the bar an object was created on.

    Unused persist-path kwargs: generated persist/trace never pops keys.
    DateTime arithmetic runs only when the 8-arg strategy's code object
    contains the string ``'datetime'`` (or when the strategy takes
    ``**kwargs``, fail-closed). Daily OpenD/HighD/LowD/CloseD tracking,
    SessionLastBar, CurrentSession, unused FillEngine position fields,
    and unused ``current_date`` / ``current_time`` / ``currentbar`` /
    ``barnumber`` / ``outpath`` / ``_first_bar`` writes follow the same
    rule — skip the per-bar write when the 8-arg body never names the
    key. Config / LastCalc / len / prec are restored only for keys the
    previous bar's ``per_bar_extra`` overrode, so a key the extra omits
    reads its default. Hand-written ``**kwargs``
    still gets DateTime, daily OHLC, session, every position field, and
    the bar-id keys every bar.
    """
    if strict is None:
        strict = os.environ.get("PL_STRICT", "").strip().lower() not in (
            "", "0", "false", "no", "off")
    trade_from, max_bars_back = _validate_window(trade_from, max_bars_back, n_bars)
    pass_kw_dict = _takes_kwargs_dict(strategy_fn)
    needs = _kwarg_needs(strategy_fn, pass_kw_dict)
    hist = _init_history(init_hist)
    if needs.session_last and session_end_tm is None:
        session_end_tm = _infer_session_end_tm(dates, times, n_bars)
    if lastcalc_kwargs is None:
        lastcalc_kwargs = _lastcalc_kwargs(dates, times, n_bars)
    # O(1) `_state['x'] + [x]` lookback views; see runtime/_rt_history.
    state = SeriesState()
    const_defaults = _const_defaults(config, lastcalc_kwargs)
    call_kwargs = _initial_call_kwargs(
        config, lastcalc_kwargs, state, fill_engine.closed_trades)
    return _drive_bars(
        strategy_fn, pass_kw_dict, needs, hist, call_kwargs, const_defaults,
        state, n_bars=n_bars, get_ohlcv=get_ohlcv, dates=dates, times=times,
        fill_engine=fill_engine, emit_row=emit_row, warmup=warmup,
        per_bar_extra=per_bar_extra, session_end_tm=session_end_tm,
        strict=strict, trade_from=trade_from, emit_trace=emit_trace,
        max_bars_back=max_bars_back, on_bar=on_bar)


def _drive_bars(
    strategy_fn: Callable[..., Any],
    pass_kw_dict: bool,
    needs: _KwargNeeds,
    hist: _History,
    call_kwargs: dict[str, Any],
    const_defaults: dict[str, Any],
    state: SeriesState,
    *,
    n_bars: int,
    get_ohlcv: Callable[[int], Any],
    dates: Any,
    times: Any,
    fill_engine: "FillEngine",
    emit_row: Callable[[int, int, dict[str, Any]], Any],
    warmup: int,
    per_bar_extra: Optional[Callable[[int], Optional[dict[str, Any]]]],
    session_end_tm: Optional[int],
    strict: bool,
    trade_from: int,
    emit_trace: bool,
    max_bars_back: int,
    on_bar: Optional[Callable[[int], Any]],
) -> tuple[int, int, Optional[str]]:
    """The per-bar loop: history, fills, kwargs, strategy call, emit.

    Hot path: flags and buffers are bound to locals up front, and the common
    per-bar work (including the position kwargs) stays inline; only work a
    strategy opts into (DateTime, daily OHLC, SessionLastBar,
    ``per_bar_extra``) goes through small helpers.
    """
    o_hist, h_hist, l_hist, c_hist, v_hist, d_hist, t_hist = hist
    _pass_kw_dict = pass_kw_dict
    _need_datetime = needs.datetime
    _need_daily = needs.daily
    _need_session_last = needs.session_last
    _need_currentsession = needs.currentsession
    _need_all_pos = needs.all_pos
    _need_mp = needs.mp
    _need_cc = needs.cc
    _pos_extra = needs.pos_extra
    _need_current_date = needs.current_date
    _need_current_time = needs.current_time
    _need_currentbar = needs.currentbar
    _need_barnumber = needs.barnumber
    _need_outpath = needs.outpath
    _need_first_bar = needs.first_bar
    _track_daily = _need_daily or _need_currentsession
    _engine_keys = frozenset(call_kwargs)

    barnumber = 0
    first_bar = True
    _throw_count = 0
    _total_strategy_calls = 0
    _first_traceback: Optional[str] = None

    prev_close_for_exit = None
    prev_dt_for_exit = 0
    prev_tm_for_exit = 0
    prev_date = None
    daily = _DailyOHLC()
    _hook_only: list[str] = []
    _restore_consts: tuple[str, ...] = ()

    for i in range(n_bars):
        o, h, l, c, v = get_ohlcv(i)
        dt = dates[i]
        tm = times[i]
        history_only = i < max_bars_back
        if not history_only:
            barnumber += 1

        o_hist.append(o); h_hist.append(h); l_hist.append(l); c_hist.append(c)
        v_hist.append(v); d_hist.append(dt); t_hist.append(tm)

        # Seeded warmup (trade_from): strategy + history + _state always run.
        # Fills (before_bar / SetExitOnClose) start at trade_from. The last warmup
        # bar may still queue after_bar orders so a "Buy next bar" on the bar
        # before the window fills on the first trading bar (full-tape timing).
        trading = i >= trade_from and not history_only
        queue_orders = i >= trade_from - 1

        # SetExitOnClose: close the position at the previous bar's close when the
        # date changes (EL closes all positions on the last bar of the session).
        if (trading and prev_date is not None and dt != prev_date and
                fill_engine._risk.get('exit_on_close', False) and
                prev_close_for_exit is not None):
            _exit_on_close(fill_engine, prev_close_for_exit, barnumber,
                           prev_dt_for_exit, prev_tm_for_exit)

        # Persist 8-arg path skips daily tracking when the body never names those keys.
        if _track_daily:
            daily.update(prev_date, dt, o, h, l, c)
        prev_date = dt
        if history_only:
            continue

        # Process fills BEFORE strategy (fills pending orders from previous bar).
        if trading:
            fill_engine.before_bar(o, c, h, l, barnumber, date_str=dt, time_str=tm)

        if _need_current_date:
            call_kwargs['current_date'] = dt
        if _need_current_time:
            call_kwargs['current_time'] = tm
        if _need_datetime:
            call_kwargs['datetime'] = _el_datetime(dt, tm)
        # Daily OHLC (computed from bar data, not from any capture column).
        if _need_daily:
            call_kwargs['opend_0'] = daily.open
            call_kwargs['highd_0'] = daily.high
            call_kwargs['lowd_0'] = daily.low
            call_kwargs['closed_0'] = daily.close
            call_kwargs['closed_1'] = daily.prev_close
        if _need_currentsession:
            call_kwargs['currentsession'] = daily.session
        if _need_currentbar:
            call_kwargs['currentbar'] = barnumber
        if _need_session_last:
            call_kwargs['session_last_bar'] = _session_last_bar(
                dates, i, n_bars, dt, tm, session_end_tm)
        if per_bar_extra is not None:
            _restore_consts = _apply_bar_extra(
                call_kwargs, per_bar_extra(i), _engine_keys, const_defaults,
                _hook_only, _restore_consts)
        # Position kwargs, then bar-id keys, are written after extra so they
        # win over it. Persist 8-arg path writes only the keys the body names.
        if _need_all_pos:
            fill_engine.fill_pos_kwargs(call_kwargs)
        else:
            if _need_mp:
                call_kwargs['market_position'] = fill_engine.market_position
            if _need_cc:
                call_kwargs['current_contracts'] = fill_engine.contracts
            for _k, _a in _pos_extra:
                call_kwargs[_k] = getattr(fill_engine, _a)
        if _need_barnumber:
            call_kwargs['barnumber'] = barnumber
        if _need_outpath:
            call_kwargs['outpath'] = ''
        if _need_first_bar:
            call_kwargs['_first_bar'] = first_bar
        call_kwargs['_state'] = state
        call_kwargs['time_s'] = tm * 100

        trace = {}
        _total_strategy_calls += 1
        if on_bar is not None:
            on_bar(i)
        try:
            if _pass_kw_dict:
                result = strategy_fn(
                    o_hist, h_hist, l_hist, c_hist, v_hist, d_hist, t_hist,
                    call_kwargs,
                )
            else:
                result = strategy_fn(
                    o_hist, h_hist, l_hist, c_hist, v_hist, d_hist, t_hist,
                    **call_kwargs,
                )
            trace = result.get('_trace', {})
            state = result.get('_state', state)
            orders = result.get('orders', [])
            risk = result.get('risk', {})
            if queue_orders:
                fill_engine.after_bar(
                    orders, risk, c, barnumber=barnumber, date_str=dt, time_str=tm)
            prev_close_for_exit = c
            prev_dt_for_exit = dt
            prev_tm_for_exit = tm
        except Exception as e:
            # FAIL-LOUD: count every per-bar throw, capture the first traceback,
            # and (under PL_STRICT) re-raise immediately. An unimplemented
            # builtin always stops the run: it only raises in strict mode.
            _throw_count += 1
            if _first_traceback is None:
                _first_traceback = traceback.format_exc()
            if strict or isinstance(e, UnimplementedBuiltinError):
                raise
            if _throw_count <= 3:
                print(f"  throw#{_throw_count} bar {barnumber}: {type(e).__name__}: {e}")

        first_bar = False

        # Skip output for GT `warmup` bars and for the seeded-warmup prefix.
        if i < trade_from or (warmup > 0 and i < warmup):
            continue

        if emit_trace:
            fill_engine.inject_trace(trace)
        emit_row(i, barnumber, trace)

    return _throw_count, _total_strategy_calls, _first_traceback
