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
import os
import traceback

from pl_transpiler.engine_dates import _el_date_to_day_of_week


# FillEngine.fill_pos_kwargs keys → attribute names. Persist 8-arg path
# writes only the keys the strategy names; **kwargs still writes the full set.
_POS_KWARGS = (
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
)
_DAILY_OHLC_KEYS = ('opend_0', 'highd_0', 'lowd_0', 'closed_0', 'closed_1')


def _code_has_const(fn, name):
    """True if *name* is a string constant in *fn* or a nested code object.

    Generated persist/trace reads kwargs via ``kwargs.get('key', default)``, so
    the key is a ``co_consts`` string. Fail closed (True) when *fn* cannot be
    inspected — compute the value rather than skip it.
    """
    code = getattr(fn, "__code__", None)
    if code is None:
        return True
    stack = [code]
    seen = set()
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


def run_bar_loop(
    strategy_fn,
    *,
    n_bars,
    get_ohlcv,
    dates,
    times,
    config,
    fill_engine,
    emit_row,
    warmup=0,
    init_hist=None,
    per_bar_extra=None,
    lastcalc_kwargs=None,
    session_end_tm=None,
    strict=None,
    trade_from=0,
    emit_trace=True,
):
    """Shared per-bar execution core used by BOTH run_gt (the GT capture harness)
    and the general entrypoint (tools/pl_run.run_el).

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

    trade_from: first tape index that may fill or emit (issue #12 seeded warmup).
    Bars before it still run the strategy, grow history, and persist `_state`
    (Once / Average / Close[1] / Variables see the prefix), but FillEngine fills
    are suppressed. The last warmup bar may queue "next bar" orders so a signal
    on the bar before the window still fills on the first trading bar. Default 0
    is byte-identical to the previous loop. Independent of `warmup`, which only
    skips output rows (GT capture alignment) and still processes fills.

    emit_trace: when True (default), FillEngine.inject_trace writes position
    columns into the per-bar trace dict before emit_row — required for run_el /
    GT CSV output. persist min-emit (pl_oracle, scoring A/B) does not read that
    dict; pass False to skip the 14 unused stores/bar. Fills and `_state` are
    unchanged. Default True is the previous loop.

    Unused persist-path kwargs: generated persist/trace never pops keys.
    DateTime arithmetic runs only when the 8-arg strategy's code object
    contains the string ``'datetime'`` (or when the strategy takes
    ``**kwargs``, fail-closed). Daily OpenD/HighD/LowD/CloseD tracking,
    SessionLastBar, CurrentSession, unused FillEngine position fields,
    and unused ``current_date`` / ``current_time`` / ``currentbar`` /
    ``barnumber`` / ``outpath`` / ``_first_bar`` writes follow the same
    rule — skip the per-bar write when the 8-arg body never names the
    key. Config / LastCalc / len / prec are restored only for keys the
    previous bar's ``per_bar_extra`` overrode, so an omitted extra still
    matches the old rebuilt-dict defaults. Hand-written ``**kwargs``
    still gets DateTime, daily OHLC, session, every position field, and
    the bar-id keys every bar.
    """
    import datetime as _dt
    from collections import Counter
    if strict is None:
        strict = bool(os.environ.get("PL_STRICT"))

    trade_from = int(trade_from or 0)
    if trade_from < 0:
        raise ValueError(f"trade_from must be >= 0, got {trade_from}")
    if trade_from > n_bars:
        raise ValueError(f"trade_from {trade_from} exceeds n_bars {n_bars}")

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
    # Persist/trace 8-arg path: skip unused kwargs work when the generated
    # body never names the key. **kwargs callers (tests, dumps) always compute.
    if not _pass_kw_dict:
        _need_datetime = True
        _need_daily = True
        _need_session_last = True
        _need_currentsession = True
        _need_all_pos = True
        _need_mp = False
        _need_cc = False
        _pos_extra = ()
        _need_current_date = True
        _need_current_time = True
        _need_currentbar = True
        _need_barnumber = True
        _need_outpath = True
        _need_first_bar = True
    else:
        _need_datetime = _code_has_const(strategy_fn, "datetime")
        _need_daily = any(
            _code_has_const(strategy_fn, k) for k in _DAILY_OHLC_KEYS)
        _need_session_last = _code_has_const(strategy_fn, "session_last_bar")
        _need_currentsession = _code_has_const(strategy_fn, "currentsession")
        _pos_needed = tuple(
            (k, a) for k, a in _POS_KWARGS
            if _code_has_const(strategy_fn, k)
        )
        _need_all_pos = len(_pos_needed) == len(_POS_KWARGS)
        _need_mp = (not _need_all_pos) and any(
            k == "market_position" for k, _a in _pos_needed)
        _need_cc = (not _need_all_pos) and any(
            k == "current_contracts" for k, _a in _pos_needed)
        _pos_extra = tuple(
            p for p in _pos_needed
            if p[0] not in ("market_position", "current_contracts")
        )
        _need_current_date = _code_has_const(strategy_fn, "current_date")
        _need_current_time = _code_has_const(strategy_fn, "current_time")
        _need_currentbar = _code_has_const(strategy_fn, "currentbar")
        _need_barnumber = _code_has_const(strategy_fn, "barnumber")
        _need_outpath = _code_has_const(strategy_fn, "outpath")
        _need_first_bar = _code_has_const(strategy_fn, "_first_bar")
    _track_daily = _need_daily or _need_currentsession

    # Growing-history buffers. PAD=1 leading zero mirrors run_gt; init_hist lets a
    # caller pre-seed real pre-capture warmup bars (GTA5 Data1).
    # Issue #13: these stay Python lists. Isolated FAST-LANE (856,914 bars):
    # array.array append+[-1] is ~1.2× slower (re-boxes a float on every read);
    # a Python __getitem__ wrapper is ~2.7× slower; MaxBarsBack pop(0) cap is
    # noise on Audit Ref 3. strategy() wants Python floats; list.append stores the
    # tape's existing PyFloat. A contiguous buffer is the right FFI layout for
    # the optional accel extra (#18) — do not silently switch this path.
    if init_hist is not None:
        o_hist = init_hist['o']; h_hist = init_hist['h']; l_hist = init_hist['l']
        c_hist = init_hist['c']; v_hist = init_hist['v']
        d_hist = init_hist['d']; t_hist = init_hist['t']
    else:
        PAD = 1
        o_hist, h_hist, l_hist, c_hist = ([0.0] * PAD for _ in range(4))
        v_hist, d_hist, t_hist = ([0] * PAD for _ in range(3))

    # Canonical session-end time-of-day (most common time-of-day at a day boundary),
    # used to classify the FINAL bar's SessionLastBar (no next bar to compare).
    # Derived from the date/time series so it stays instrument-agnostic.
    # Skipped when the 8-arg persist body never names session_last_bar.
    if _need_session_last and session_end_tm is None and n_bars > 1:
        _se = Counter()
        for _j in range(n_bars - 1):
            if dates[_j] != dates[_j + 1]:
                _se[times[_j]] += 1
        if _se:
            session_end_tm = _se.most_common(1)[0][0]

    # LastCalc*: datetime of the FINAL bar of the dataset (constant across bars).
    if lastcalc_kwargs is None:
        lastcalc_kwargs = {}
        if n_bars > 0:
            try:
                _ld = int(dates[n_bars - 1]); _lt = int(times[n_bars - 1])
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

    barnumber = 0
    first_bar = True
    state = {}
    _throw_count = 0
    _total_strategy_calls = 0
    _first_traceback = None

    prev_close_for_exit = None
    prev_dt_for_exit = 0
    prev_tm_for_exit = 0
    prev_date = None

    # Daily OHLC tracking for OpenD/HighD/LowD/CloseD.
    day_open = 0.0
    day_high = 0.0
    day_low = 0.0
    day_close = 0.0
    prev_day_close = -1  # CloseD(1): yesterday's close; -1 when no prior day
    current_session = 2  # dummy; recomputed on first bar below

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
        'current_date': 0,
        'current_time': 0,
        'datetime': 0.0,
        'opend_0': 0.0,
        'highd_0': 0.0,
        'lowd_0': 0.0,
        'closed_0': 0.0,
        'closed_1': -1,
        'currentsession': current_session,
        'currentbar': 0,
        'session_last_bar': 0,
        'barnumber': 0,
        '_first_bar': True,
        '_state': state,
        # Closed-position ledger for EntryDate(n)/ExitPrice(n)/... (pl_pos_ago).
        # The engine appends to this list in place, so one reference suffices.
        '_closed_trades': fill_engine.closed_trades,
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
    }
    if lastcalc_kwargs:
        call_kwargs.update(lastcalc_kwargs)
    _engine_keys = frozenset(call_kwargs)
    _hook_only = []

    # Constants never change unless extra overrides them. Restore only those
    # keys next bar (old path re-wrote all 10 + lastcalc every bar).
    _const_defaults = {
        'sess1starttime': config.sess1starttime,
        'barinterval': config.barinterval,
        'bartype': 1,
        'tradedate': 101,
        'pointvalue': config.pointvalue,
        'bigpointvalue': config.bigpointvalue,
        'minmove': config.minmove,
        'pricescale': config.pricescale,
    }
    if lastcalc_kwargs:
        _const_defaults.update(lastcalc_kwargs)
    _restore_consts = ()

    for i in range(n_bars):
        o, h, l, c, v = get_ohlcv(i)
        dt = dates[i]
        tm = times[i]
        barnumber += 1

        o_hist.append(o); h_hist.append(h); l_hist.append(l); c_hist.append(c)
        v_hist.append(v); d_hist.append(dt); t_hist.append(tm)

        # Seeded warmup (trade_from): strategy + history + _state always run.
        # Fills (before_bar / SetExitOnClose) start at trade_from. The last warmup
        # bar may still queue after_bar orders so a "Buy next bar" on the bar
        # before the window fills on the first trading bar (full-tape timing).
        trading = i >= trade_from
        queue_orders = i >= trade_from - 1

        # SetExitOnClose: close the position at the previous bar's close when the
        # date changes (EL closes all positions on the last bar of the session).
        cur_date = dt
        if (trading and prev_date is not None and cur_date != prev_date and
                fill_engine._risk.get('exit_on_close', False) and
                prev_close_for_exit is not None):
            # A 'this bar on close' order placed on the prior (last session) bar fills
            # FIRST at that close — it may REVERSE the position — and THEN exit-on-close
            # flattens whatever remains. MC counts BOTH (verified o3 bn66595->66596:
            # SE_CLS reverses long->short (+1 trade), exit-on-close closes the short
            # (+1), session ends flat with tot 2526->2528). Both fills price at the
            # last bar's close. _execute_onclose_pending clears its own queue, so the
            # subsequent before_bar call is a no-op.
            fill_engine._execute_onclose_pending()
            if fill_engine.market_position != 0:
                fill_engine._close_at_stop(prev_close_for_exit, 'exit_on_close', barnumber - 1,
                                           exit_date=prev_dt_for_exit, exit_time=prev_tm_for_exit,
                                           exit_name='End of Day Exit', exit_cat=4)

        # Track daily OHLC for OpenD/HighD/LowD/CloseD / CurrentSession.
        # Persist 8-arg path skips this when the body never names those keys.
        if _track_daily:
            if prev_date is None:
                day_open = o
                day_high = h
                day_low = l
                prev_day_close = -1
                current_session = _el_date_to_day_of_week(dt)
            elif cur_date != prev_date:
                prev_day_close = day_close
                current_session = _el_date_to_day_of_week(dt)
                day_open = o
                day_high = h
                day_low = l
            else:
                day_high = max(day_high, h)
                day_low = min(day_low, l)
            day_close = c
        prev_date = cur_date

        # Process fills BEFORE strategy (fills pending orders from previous bar).
        if trading:
            fill_engine.before_bar(o, c, h, l, barnumber, date_str=dt, time_str=tm)

        if _need_current_date:
            call_kwargs['current_date'] = dt
        if _need_current_time:
            call_kwargs['current_time'] = tm
        # DateTime (YYYYMMDD.frac) for DateTime-decomposition functions.
        if _need_datetime:
            yyyy = 1900 + (dt // 10000)
            mm = (dt // 100) % 100
            dd = dt % 100
            date_part = yyyy * 10000 + mm * 100 + dd
            hh = tm // 100
            mm_t = tm % 100
            call_kwargs['datetime'] = date_part + (hh * 3600 + mm_t * 60) / 86400.0

        # Daily OHLC (computed from bar data, not from any capture column).
        if _need_daily:
            call_kwargs['opend_0'] = day_open
            call_kwargs['highd_0'] = day_high
            call_kwargs['lowd_0'] = day_low
            call_kwargs['closed_0'] = day_close
            call_kwargs['closed_1'] = prev_day_close

        if _need_currentsession:
            call_kwargs['currentsession'] = current_session
        if _need_currentbar:
            call_kwargs['currentbar'] = barnumber
        # SessionLastBar: this bar is the session's last bar when the next bar
        # belongs to a new day; the final bar is classified by time-of-day.
        if _need_session_last:
            if i + 1 < n_bars:
                call_kwargs['session_last_bar'] = 1 if dates[i + 1] != dt else 0
            else:
                call_kwargs['session_last_bar'] = (
                    1 if (session_end_tm is not None and tm == session_end_tm) else 0)

        # Restore only const keys extra overrode last bar, so an omitted extra
        # still matches a rebuilt dict. Generated persist never pops; **kwargs
        # pops a copy and cannot hole call_kwargs.
        if _restore_consts:
            _cd = _const_defaults
            _ck = call_kwargs
            for _k in _restore_consts:
                _ck[_k] = _cd[_k]
            _restore_consts = ()

        # Drop hook-only keys from the previous bar so a missing capture column
        # cannot leak (the old extra_kwargs dict was rebuilt every bar).
        if _hook_only:
            for k in _hook_only:
                call_kwargs.pop(k, None)
            _hook_only.clear()

        # Capture-specific extras (tick fields, Data2, Len/Prec). Generic values
        # set above always win for keys the hook does not provide.
        if per_bar_extra is not None:
            extra = per_bar_extra(i)
            if extra:
                call_kwargs.update(extra)
                ek = _engine_keys
                for k in extra:
                    if k not in ek:
                        _hook_only.append(k)
                _restore_consts = tuple(k for k in extra if k in _const_defaults)

        # Position kwargs last so they win over extra on overlap (**pos_kwargs
        # used to unpack after **extra_kwargs). Persist 8-arg path writes only
        # the keys the body names.
        if _need_all_pos:
            fill_engine.fill_pos_kwargs(call_kwargs)
        else:
            if _need_mp:
                call_kwargs['market_position'] = fill_engine.market_position
            if _need_cc:
                call_kwargs['current_contracts'] = fill_engine.contracts
            if _pos_extra:
                _ck = call_kwargs
                _fp = fill_engine
                for _k, _a in _pos_extra:
                    _ck[_k] = getattr(_fp, _a)

        # These used to be explicit keyword args, so they win over extra.
        # Persist 8-arg path skips keys the body never names.
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
            # and (under PL_STRICT) re-raise immediately.
            _throw_count += 1
            if _first_traceback is None:
                _first_traceback = traceback.format_exc()
            if strict:
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
