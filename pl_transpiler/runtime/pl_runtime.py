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
PowerLanguage runtime helpers.
These are called by transpiled Python code at execution time.
Implementer agents add functions here as constructs are implemented.
"""

import struct

def f32(x):
    """Round-trip through IEEE-754 float32 to match EasyLanguage single-precision."""
    if x is None:
        return 0.0
    try:
        return struct.unpack('f', struct.pack('f', float(x)))[0]
    except (OverflowError, struct.error, ValueError, TypeError):
        return float(x)

def el_round0(x):
    """EasyLanguage Round(x, 0): half-away-from-zero (NOT Python banker's rounding)."""
    import math
    if x >= 0:
        return math.floor(x + 0.5)
    else:
        return math.ceil(x - 0.5)

def el_round(x, decimals):
    """EasyLanguage Round(x, decimals): generalised half-away-from-zero."""
    import math
    factor = 10 ** decimals
    if x >= 0:
        return math.floor(x * factor + 0.5) / factor
    else:
        return math.ceil(x * factor - 0.5) / factor

# --- Unimplemented-stub instrumentation -------------------------------------
# Many EasyLanguage builtins are stubs: charting/UI, broker/account, DLL,
# portfolio, collections (all out of scope for a backtest), plus not-yet-built
# calc functions. To avoid SILENTLY wrong results, every stub records its call
# here and, under PL_STRICT, raises -- so an LLM-written strategy that touches an
# unimplemented function fails LOUDLY instead of producing plausible-but-wrong
# trades. Default (non-strict) keeps the historical behaviour (return 0) but
# tracks the call for the coverage report (tools/strategy_report.py).
import os as _os
_PL_STRICT = _os.environ.get('PL_STRICT', '').strip().lower() not in ('', '0', 'false', 'no')
_UNIMPL_CALLS = {}
# Stubs that are legitimately no-ops stay silent even under strict mode. Keep
# this minimal and explicit; empty by default so nothing is hidden.
_UNIMPL_WHITELIST = set()

# String-returning EL builtins. When these are unimplemented their loud stub
# must return '' (not the numeric 0 default), otherwise transpiled code that
# concatenates the result (Line = Line + Symbol) raises TypeError mid-bar and
# aborts the whole bar, zeroing every traced column. The non-strict fallback is
# type-correct; the strict path still raises so the gap stays visible.
_UNIMPL_STR_RETURNING = {
    'pl_symbol', 'pl_symbolname', 'pl_description',
    'pl_datetimetostring', 'pl_datetimetostring_ms',
    'pl_formatdate', 'pl_formattime', 'pl_timetostring', 'pl_datetostring',
    'pl_array_getstringvalue',
}

def _unimplemented(name):
    _UNIMPL_CALLS[name] = _UNIMPL_CALLS.get(name, 0) + 1
    if _PL_STRICT and name not in _UNIMPL_WHITELIST:
        raise NotImplementedError(
            "EasyLanguage function '%s' is not implemented in the transpiler runtime "
            "(out-of-scope charting/UI/broker/portfolio, or not yet built) -- its value "
            "cannot be trusted. Run without PL_STRICT to treat it as 0 and continue." % name)
    return '' if name in _UNIMPL_STR_RETURNING else 0

def pl_unimplemented_report():
    """{name: call_count} of unimplemented functions hit since the last reset (coverage linter)."""
    return dict(_UNIMPL_CALLS)

def pl_unimplemented_reset():
    """Clear the unimplemented-call registry (call before a fresh strategy run)."""
    _UNIMPL_CALLS.clear()


def pl_pos_trade_field(kwargs, field, pos_ago=0, trade_number=0):
    """2-arg position keyword, e.g. PosTradeProfit(PosAgo, TradeNumber).

    Looks up a per-trade value supplied by the runner via kwargs. The runner
    may key these either generically (just the field name) or per (pos_ago,
    trade_number). Until the capture-driven per-trade supply is wired up
    (a later phase), this defaults to 0 so transpiled code executes.
    """
    try:
        pos_ago = int(pos_ago)
        trade_number = int(trade_number)
    except (TypeError, ValueError):
        pos_ago, trade_number = 0, 0
    keyed = kwargs.get((field, pos_ago, trade_number))
    if keyed is not None:
        return keyed
    # String-typed trade fields (names/categories rendered as text) must default
    # to '' so callers that concatenate them (Line = Line + PosTradeEntryName(...))
    # don't raise TypeError and abort the bar, zeroing the whole trace.
    default = '' if field in ('postradeentryname', 'postradeexitname') else 0
    return kwargs.get(field, default)


def _valid_window(series, length):
    """Return the last `length` values from series, skipping leading padding zeros.
    Returns (window, valid_count) where valid_count is the number of non-padding
    elements. If fewer than `length` valid elements exist, returns (window, <length)."""
    if not series or length <= 0:
        return [], 0
    start = _find_first_valid(series)
    n_valid = len(series) - start
    # Only the last `length` valid values are ever needed. Slice them directly
    # instead of copying the whole `series[start:]` tail every call — that copy
    # made this O(n) per bar (O(n^2) over a full run, the GT2 full-range wall).
    # Padding lives only at the front, so when n_valid >= length the last
    # `length` elements are all valid and series[-length:] is byte-identical to
    # the old valid[-length:].
    if n_valid >= length:
        return series[-length:], length
    return series[start:], n_valid

def pl_average(series, length):
    """Simple moving average of last `length` values in series.
    Skips leading padding zeros."""
    window, n = _valid_window(series, length)
    if n < length or n == 0:
        return 0.0
    return f32(sum(window) / length)

def pl_highest(series, length):
    """Highest value over last `length` bars."""
    if not series or length <= 0:
        return 0.0
    window = series[-length:] if len(series) >= length else series
    return f32(max(window))

def pl_lowest(series, length):
    """Lowest value over last `length` bars."""
    if not series or length <= 0:
        return 0.0
    window = series[-length:] if len(series) >= length else series
    return f32(min(window))

def _cross_prior_relation(series_a, series_b, start):
    """Return the sign of (A - B) on the bar that establishes the pre-cross
    relation, walking back through any consecutive run of equal bars
    (pdf:2502-2509 / 2580-2587). EL defines a cross relative to the last bar
    BEFORE the current one on which A and B differed: a run of equal bars
    immediately preceding the current bar is transparent, and the relation is
    taken from the bar before that run. Returns -1 if A<B, +1 if A>B, 0 if no
    differing prior bar exists (all-equal history -> no cross)."""
    i = len(series_a) - 2  # bar immediately preceding the current bar
    while i >= start:
        a, b = series_a[i], series_b[i]
        if a < b:
            return -1
        if a > b:
            return 1
        i -= 1  # equal on this bar: keep walking back through the equal run
    return 0

def pl_crosses_above(series_a, series_b):
    """True if series_a crossed above series_b on this bar.
    Skips leading padding zeros so the first real bar never fires a false cross.
    A run of equal bars immediately preceding the current bar is transparent;
    the pre-cross relation is taken from the bar before that run (pdf:2502-2509)."""
    if not hasattr(series_a, '__len__') or not hasattr(series_b, '__len__'):
        return False
    if len(series_a) < 2 or len(series_b) < 2:
        return False
    start = max(_find_first_valid(series_a), _find_first_valid(series_b))
    if len(series_a) - start < 2:
        return False
    return series_a[-1] > series_b[-1] and _cross_prior_relation(series_a, series_b, start) < 0

def pl_crosses_below(series_a, series_b):
    """True if series_a crossed below series_b on this bar.
    Skips leading padding zeros so the first real bar never fires a false cross.
    A run of equal bars immediately preceding the current bar is transparent;
    the pre-cross relation is taken from the bar before that run (pdf:2561-2569)."""
    if not hasattr(series_a, '__len__') or not hasattr(series_b, '__len__'):
        return False
    if len(series_a) < 2 or len(series_b) < 2:
        return False
    start = max(_find_first_valid(series_a), _find_first_valid(series_b))
    if len(series_a) - start < 2:
        return False
    return series_a[-1] < series_b[-1] and _cross_prior_relation(series_a, series_b, start) > 0

def pl_true_range(high, low, prev_close):
    """True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))"""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

# ---- Incremental indicator cache (#7b vectorization) ----
# Recursive indicators (RSI/EMA) accumulate over the FULL history from the first
# valid bar, so recomputing from scratch every bar is O(n^2). Because each series
# (e.g. c_hist) is a single append-only list within one run_gt run, we carry the
# recursion state keyed by (fn, id(series), length) and extend it by the new bars
# only. This reproduces the EXACT same float op sequence — just without redoing the
# prefix. CRITICAL: reset between runs (run_gt calls reset_indicator_caches()),
# because verify_all reuses the process and a GC'd list's id() can be recycled.
_ind_cache = {}


def reset_indicator_caches():
    """Clear the incremental indicator state. MUST be called at the start of every
    run_gt run so a recycled list id() can never serve a stale prior-run cache."""
    _ind_cache.clear()


def pl_partial_stub(name, line):
    """Partial-transpile stub factory (FL2 opt-in --partial mode).

    Returns a callable that ALWAYS raises UnimplementedKeywordError the moment it
    is evaluated — every time, with no PL_STRICT gating and no default value. This
    is the execution-time replacement codegen emits (only under partial=True) for
    an EL keyword that has no runtime implementation, so a partial transpile can
    compile and run the implemented portion while making any reliance on the
    unimplemented construct fail loud instead of silently returning a wrong value.

    UnimplementedKeywordError is imported from pl_transpiler.errors (never from
    codegen) to avoid an import cycle."""
    from pl_transpiler.errors import UnimplementedKeywordError

    def _stub(*args, **kwargs):
        where = f" at line {line}" if line is not None else ""
        raise UnimplementedKeywordError(
            f"unimplemented EL keyword '{name}'{where} (partial-mode stub executed)")

    return _stub


def pl_rsi(series, length):
    """RSI over `length` periods using Wilder's smoothing (recursive).
    Skips leading padding zeros to match EL behavior.
    NUMERIC MODEL: EL/PowerLanguage computes indicator internals in DOUBLE precision
    and rounds only the FINAL result to float32 (the same model proven by GT1's
    runsum/double-cumsum capture). The former f32-per-step model stayed within
    tolerance at ~1500 bars but drifted past it for the long-memory Wilder recursion
    at full range (GT1 rsi diverged ~5e-6 by bar ~107k). So the Wilder accumulation
    runs in double; inputs are f32 (prices are f32-stored) and the return is f32.
    Incremental: carries (start, gain-index, seed sums, avg_gain/avg_loss) across
    bars; byte-identical to a double full-history recompute."""
    n = len(series)
    if n < length + 1:
        return f32(50.0)
    key = ('rsi', id(series), length)
    st = _ind_cache.get(key)
    if st is None or st.get('n', 0) > n:
        start = 0
        while start < n and series[start] == 0:
            start += 1
        st = {'start': start, 'next': start + 1, 'gi': 0,
              'seed_g': 0.0, 'seed_l': 0.0, 'avg_g': None, 'avg_l': None, 'n': 0}
        _ind_cache[key] = st
    start = st['start']
    if start >= n:
        return f32(50.0)
    i = st['next']
    while i < n:
        diff = f32(series[i]) - f32(series[i - 1])
        g = max(diff, 0.0)
        l = max(-diff, 0.0)
        gi = st['gi']
        if gi < length:
            # seed sums (== sum(gains[:length]) in order); finalize at gi==length-1
            st['seed_g'] += g
            st['seed_l'] += l
            if gi == length - 1:
                st['avg_g'] = st['seed_g'] / length
                st['avg_l'] = st['seed_l'] / length
        else:
            st['avg_g'] = (st['avg_g'] * (length - 1) + g) / length
            st['avg_l'] = (st['avg_l'] * (length - 1) + l) / length
        st['gi'] += 1
        i += 1
    st['next'] = n
    st['n'] = n
    if st['gi'] < length:
        return f32(50.0)
    avg_gain = st['avg_g']
    avg_loss = st['avg_l']
    if avg_loss == 0:
        return f32(100.0)
    rs = avg_gain / avg_loss
    return f32(100.0 - 100.0 / (1.0 + rs))

def pl_momentum(series, length):
    """Momentum = current value - value N bars ago.
    Skips leading padding zeros."""
    window, n = _valid_window(series, length + 1)
    if n < length + 1:
        return 0.0
    return f32(window[-1] - window[0])

def pl_summation(series, length):
    """Sum of last `length` values.
    Skips leading padding zeros."""
    window, n = _valid_window(series, length)
    if n < length or n == 0:
        return 0.0
    return f32(sum(window))

def pl_std_dev(series, length, data_type=1):
    """Standard deviation of last `length` values.
    data_type: 1=population (divide by N), 2=sample (divide by N-1).
    Skips leading padding zeros."""
    import math
    window, n = _valid_window(series, length)
    if n < length or n == 0:
        return 0.0
    mean = f32(sum(window) / length)
    variance = f32(sum(f32((x - mean) ** 2) for x in window) / length)
    if data_type == 2 and length > 1:
        return f32(math.sqrt(f32(variance * length / (length - 1))))
    return f32(math.sqrt(variance))


# ---- Technical Indicators ----

def _ema(series, length):
    """Exponential moving average helper.
    EL semantics: first value = price, then k*price + (1-k)*prev.
    Skips leading padding zeros to match EL behavior.
    Incremental: carries the running ema_val across bars (keyed by id(series),
    length); byte-identical to the former series[start+1:] full recompute."""
    if not series or length <= 0:
        return 0.0
    n = len(series)
    k = 2.0 / (length + 1)
    key = ('ema', id(series), length)
    st = _ind_cache.get(key)
    if st is not None and st['n'] <= n:
        ema_val = st['ema']
        for i in range(st['n'], n):
            ema_val = series[i] * k + ema_val * (1 - k)
        st['n'] = n
        st['ema'] = ema_val
        return ema_val
    # cold: find first valid bar and fold the whole prefix once
    start = 0
    while start < n and series[start] == 0:
        start += 1
    if start >= n:
        return 0.0
    ema_val = series[start]
    for i in range(start + 1, n):
        ema_val = series[i] * k + ema_val * (1 - k)
    _ind_cache[key] = {'n': n, 'ema': ema_val}
    return ema_val


def pl_macd(price_series, fast_len, slow_len):
    """MACD line = EMA(fast) - EMA(slow).
    EL convention: MACD(Price, FastLen, SlowLen) = MACDValue(FastLen, SlowLen, Price)"""
    if not hasattr(price_series, '__len__') or not price_series:
        return 0.0
    fast_ema = _ema(price_series, int(fast_len))
    slow_ema = _ema(price_series, int(slow_len))
    return f32(fast_ema - slow_ema)


def pl_macd_signal(price_series, fast_len, slow_len, signal_len):
    """MACD signal line = EMA of MACD line over signal_len.
    EL convention: MACDSignal(Price, FastLen, SlowLen, SignalLen)"""
    if not hasattr(price_series, '__len__') or not price_series or len(price_series) < 2:
        return 0.0
    fl, sl = int(fast_len), int(slow_len)
    macd_series = []
    for i in range(1, len(price_series) + 1):
        sub = price_series[:i]
        fast_ema = _ema(sub, fl)
        slow_ema = _ema(sub, sl)
        macd_series.append(fast_ema - slow_ema)
    return f32(_ema(macd_series, int(signal_len)))


def pl_macd_diff(price_series, fast_len, slow_len, signal_len):
    """MACD histogram = MACD - Signal.
    EL convention: MACDDiff(Price, FastLen, SlowLen, SignalLen)"""
    if not hasattr(price_series, '__len__') or not price_series:
        return 0.0
    macd_val = pl_macd(price_series, fast_len, slow_len)
    signal_val = pl_macd_signal(price_series, fast_len, slow_len, signal_len)
    return f32(macd_val - signal_val)


def pl_roc(series, length):
    """Rate of Change: (current - N ago) / N ago * 100.
    Skips leading padding zeros."""
    if not series or length <= 0:
        return 0.0
    window, n = _valid_window(series, length + 1)
    if n < length + 1:
        return 0.0
    prev = window[0]
    if prev == 0:
        return 0.0
    return f32(f32(window[-1] - prev) / prev * 100)


def pl_cci(high, low, close, length):
    """Commodity Channel Index.
    Skips leading padding zeros to match EL behavior."""
    if not close or length <= 0:
        return 0.0
    start = max(_find_first_valid(high), _find_first_valid(low), _find_first_valid(close))
    if start >= len(close):
        return 0.0
    valid = len(close) - start
    n = min(length, valid)
    if n < 1:
        return 0.0
    # n <= valid, so the last n elements are all >= start: bounded slice (no O(n) tail copy).
    hv = high[-n:]
    lv = low[-n:]
    cv = close[-n:]
    tp = []
    for i in range(-n, 0):
        tp.append((hv[i] + lv[i] + cv[i]) / 3.0)
    mean_tp = sum(tp) / len(tp)
    mean_dev = sum(abs(x - mean_tp) for x in tp) / len(tp)
    if mean_dev == 0:
        return 0.0
    return f32((tp[-1] - mean_tp) / (0.015 * mean_dev))


def pl_atr(high, low, close, length):
    """Average True Range.
    Skips leading padding zeros to match EL behavior."""
    if not high or not low or not close or length <= 0:
        return 0.0
    start = max(_find_first_valid(high), _find_first_valid(low), _find_first_valid(close))
    if start >= len(close):
        return 0.0
    valid = len(close) - start
    n = min(length, valid)
    if n < 1:
        return 0.0
    # Need the last n bars + 1 prior close (for the first TR's prev_close). Bounded
    # slice avoids the O(n) high[start:] tail copy; indices stay relative to the end.
    W = min(n + 1, valid)
    hv = high[-W:]
    lv = low[-W:]
    cv = close[-W:]
    m = len(cv)
    trs = []
    for i in range(-n, 0):
        h = hv[i]
        l = lv[i]
        idx = m + i - 1
        pc = cv[idx] if idx >= 0 else cv[0]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return f32(sum(trs) / len(trs)) if trs else 0.0


def pl_fast_k(high, low, close, length):
    """Fast %K stochastic."""
    if not close or len(close) < length:
        return 0.0
    highest_high = max(high[-length:])
    lowest_low = min(low[-length:])
    if highest_high == lowest_low:
        return 0.0
    return (close[-1] - lowest_low) / (highest_high - lowest_low) * 100


def pl_fast_d(high, low, close, length, d_len):
    """Fast %D = SMA of Fast %K over d_len periods."""
    if not close or len(close) < length + d_len:
        return 0.0
    k_values = []
    for i in range(d_len):
        offset = d_len - 1 - i
        end = len(close) - offset
        start = max(0, end - length)
        h_slice = high[start:end]
        l_slice = low[start:end]
        if not h_slice or not l_slice:
            k_values.append(0.0)
            continue
        hh = max(h_slice)
        ll = min(l_slice)
        if hh == ll:
            k_values.append(0.0)
        else:
            k_values.append((close[end - 1] - ll) / (hh - ll) * 100)
    return sum(k_values) / len(k_values)


def pl_slow_k(high, low, close, length, d_len):
    """Slow %K = Fast %D."""
    return pl_fast_d(high, low, close, length, d_len)


def pl_slow_d(high, low, close, length, d_len, slow_len):
    """Slow %D = SMA of Slow %K over slow_len periods."""
    if not close or len(close) < length + d_len + slow_len:
        return 0.0
    # The deepest lookback any inner window reads is length+d_len+slow_len bars from
    # the end, so a bounded tail yields identical values while avoiding the O(n)
    # high[:end]/low[:end]/close[:end] prefix copies (which made this O(n^2)).
    W = length + d_len + slow_len + 4
    if len(close) > W:
        high = high[-W:]
        low = low[-W:]
        close = close[-W:]
    sk_values = []
    for i in range(slow_len):
        offset = slow_len - 1 - i
        end = len(close) - offset
        sub_h = high[:end]
        sub_l = low[:end]
        sub_c = close[:end]
        sk_values.append(pl_slow_k(sub_h, sub_l, sub_c, length, d_len))
    return sum(sk_values) / len(sk_values)


def _linreg_window(series, length):
    """Get trailing window of length valid values, skipping padding zeros.
    Returns (window, n) or (None, 0) if insufficient data."""
    if not series or length <= 0:
        return None, 0
    window, n = _valid_window(series, length)
    if n < length:
        return None, 0
    return window, n


def pl_linear_reg_value(series, length, tgt_bar=0):
    """Linear regression value at current bar (tgt_bar=0) or tgt_bar bars ago.
    Skips leading padding zeros."""
    window, n = _linreg_window(series, length)
    if window is None:
        return 0.0
    # If tgt_bar != 0, shift the window
    if tgt_bar != 0:
        tgt = int(tgt_bar)
        window2, n2 = _linreg_window(series, length + tgt)
        if window2 is None:
            return 0.0
        window = window2[:length] if tgt > 0 else window2[-length:]
    sum_x = n * (n - 1) / 2
    sum_y = sum(window)
    sum_xy = sum(i * window[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return f32(intercept + slope * (n - 1))


def pl_linear_reg_slope(series, length):
    """Linear regression slope.
    Skips leading padding zeros."""
    window, n = _linreg_window(series, length)
    if window is None:
        return 0.0
    sum_x = n * (n - 1) / 2
    sum_y = sum(window)
    sum_xy = sum(i * window[i] for i in range(n))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    return f32((n * sum_xy - sum_x * sum_y) / denom)


def pl_variance(series, length):
    """Variance of last `length` values."""
    if not series or len(series) < length:
        return 0.0
    window = series[-length:]
    mean = sum(window) / length
    return sum((x - mean) ** 2 for x in window) / length


# ---- Drawing stubs ----

_drawing_id_counter = 0


def pl_tl_new(d1, t1, p1, d2, t2, p2):
    """Create a trendline, return ID."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_tl_setcolor(tl_id, color):
    pass


def pl_tl_setstyle(tl_id, style):
    pass


def pl_tl_setwidth(tl_id, width):
    pass


def pl_tl_delete(tl_id):
    pass


def pl_text_new(d, t, p, s):
    """Create a text object, return ID."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_text_setstring(text_id, s):
    pass


def pl_text_setcolor(text_id, color):
    pass


def pl_text_delete(text_id):
    pass


def pl_arw_new(d, t, p, direction):
    """Create an arrow object, return ID."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


# ---- Additional Statistical Functions ----

def pl_weighted_average(series, length):
    """Weighted moving average.
    Skips leading padding zeros."""
    window, n = _valid_window(series, length)
    if n < length or n == 0:
        return 0.0
    total_weight = n * (n + 1) / 2
    weighted_sum = sum((i + 1) * window[i] for i in range(n))
    return f32(weighted_sum / total_weight)


def pl_xaverage(series, length):
    """Exponential moving average of a series."""
    if not series or length <= 0:
        return 0.0
    return _ema(series, length)


def pl_highest_bar(series, length):
    """Returns bars ago of the highest value over length bars."""
    if not series or length <= 0:
        return 0
    window = series[-length:] if len(series) >= length else series
    max_val = max(window)
    for i in range(len(window) - 1, -1, -1):
        if window[i] == max_val:
            return len(window) - 1 - i
    return 0


def pl_lowest_bar(series, length):
    """Returns bars ago of the lowest value over length bars."""
    if not series or length <= 0:
        return 0
    window = series[-length:] if len(series) >= length else series
    min_val = min(window)
    for i in range(len(window) - 1, -1, -1):
        if window[i] == min_val:
            return len(window) - 1 - i
    return 0


def pl_correlation(series_a, series_b, length):
    """Pearson correlation coefficient between two series over `length` bars.
    Per EL docs (correlation_function_.htm): the standard linear correlation
    coefficient in [-1, 1]. Uses f32 at every step for float32 fidelity."""
    import math as _math
    if not series_a or not series_b or length <= 1:
        return f32(0.0)
    # Skip leading zeros in both series
    start_a = _find_first_valid(series_a)
    start_b = _find_first_valid(series_b)
    start = max(start_a, start_b)
    if len(series_a) < start + length or len(series_b) < start + length:
        return f32(0.0)
    # len >= start+length guarantees the last `length` values are all >= start,
    # so series[-length:] == series[start:][-length:] without the O(n) tail copy.
    a = [f32(x) for x in series_a[-length:]]
    b = [f32(x) for x in series_b[-length:]]
    n = len(a)
    if n < 2:
        return f32(0.0)
    mean_a = f32(sum(a) / n)
    mean_b = f32(sum(b) / n)
    cov = f32(sum(f32(f32(a[i] - mean_a) * f32(b[i] - mean_b)) for i in range(n)) / n)
    var_a = f32(sum(f32(f32(x - mean_a) ** 2) for x in a) / n)
    var_b = f32(sum(f32(f32(x - mean_b) ** 2) for x in b) / n)
    std_a = f32(_math.sqrt(var_a))
    std_b = f32(_math.sqrt(var_b))
    if std_a == 0 or std_b == 0:
        return f32(0.0)
    return f32(cov / f32(std_a * std_b))


# ---- Drawing Extension Stubs ----

def pl_tl_new_s(d1, t1, p1, d2, t2, p2):
    """Create trendline with seconds time."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_tl_setbegin(tl_id, d, t, p):
    pass


def pl_tl_setend(tl_id, d, t, p):
    pass


def pl_tl_setextleft(tl_id, val):
    pass


def pl_tl_setextright(tl_id, val):
    pass


def pl_tl_getbeginbar(tl_id):
    return 0


def pl_tl_getendbar(tl_id):
    return 0


def pl_tl_getbeginval(tl_id):
    return 0.0


def pl_tl_getendval(tl_id):
    return 0.0


def pl_text_new_s(d, t, p, s):
    """Create text with seconds time."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_text_setstyle(text_id, h_style, v_style):
    pass


def pl_text_setlocation(text_id, d, t, p):
    pass


def pl_text_getstring(text_id):
    return ""


def pl_text_setfontname(text_id, name):
    pass


def pl_text_setfontsize(text_id, size):
    pass


def pl_arw_new_s(d, t, p, direction):
    """Create arrow with seconds time."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


# EL built-in constants
newline = "\n"
lastbaronchart_s = True
minmove = 1  # 1 tick = 0.25 points for ES; used for chart drawing spacing


# ---- Array Functions ----

def pl_array_setmaxindex(arr, max_idx):
    """Resize array to max_idx + 1 elements. Returns True on success per EL
    (pdf:6187-6188, 6199 'True - resize successful')."""
    while len(arr) <= max_idx:
        arr.append(0)
    return True


def pl_array_getmaxindex(arr):
    """Get max valid index of array."""
    return len(arr) - 1


def pl_array_sum(arr, start, end):
    """Sum of array elements from start to end index."""
    return sum(arr[start:end + 1])


def pl_array_highest(arr, start, end):
    """Highest value in array from start to end."""
    return max(arr[start:end + 1]) if start <= end < len(arr) else 0.0


def pl_array_lowest(arr, start, end):
    """Lowest value in array from start to end."""
    return min(arr[start:end + 1]) if start <= end < len(arr) else 0.0


def pl_variancearray(arr, length):
    """Variance of first length elements (1-indexed)."""
    if length <= 0:
        return 0.0
    window = arr[1:length + 1] if len(arr) > length else arr[1:]
    if not window:
        return 0.0
    mean = sum(window) / len(window)
    return sum((x - mean) ** 2 for x in window) / len(window)


def pl_sortarray(arr, length, ascending=True):
    """Sort first length elements (1-indexed) in place."""
    if length <= 0:
        return
    sub = arr[1:length + 1]
    sub.sort(reverse=not ascending)
    arr[1:length + 1] = sub


# ---- Date / Time Functions ----

def pl_dayofweek(*args, **kwargs):
    """DayOfWeek(Date): returns 0=Sun..6=Sat."""
    if args:
        import datetime
        try:
            d = int(float(args[0]))
            y = d // 10000 + 1900
            m = (d // 100) % 100
            day = d % 100
            return datetime.date(y, m, day).weekday() + 1  # EL: 0=Sun..6=Sat
            # Python weekday(): 0=Mon..6=Sun -> convert
        except (ValueError, OverflowError):
            return 0
    return 0

def pl_month(*args, **kwargs):
    """Month(Date): returns month number (1-12)."""
    if args:
        try:
            d = int(float(args[0]))
            return (d // 100) % 100
        except (ValueError, OverflowError):
            return 0
    return 0

def pl_year(*args, **kwargs):
    """Year(Date): returns year (YYYY) or years-since-1900 depending on EL version."""
    if args:
        try:
            d = int(float(args[0]))
            y = d // 10000
            if y < 100:
                y += 1900
            return y
        except (ValueError, OverflowError):
            return 0
    return 0

def pl_dayofmonth(*args, **kwargs):
    """DayOfMonth(Date): returns day of month (1-31)."""
    if args:
        try:
            d = int(float(args[0]))
            return d % 100
        except (ValueError, OverflowError):
            return 0
    return 0

def pl_calcdate(*args, **kwargs):
    """CalcDate(RefDate, DaysChange): add/subtract days from a date.
    Returns EL date format: (year-1900)*10000 + month*100 + day"""
    if len(args) >= 2:
        import datetime
        try:
            ref = int(float(args[0]))
            delta = int(float(args[1]))
            y = ref // 10000 + 1900
            m = (ref // 100) % 100
            d = ref % 100
            dt = datetime.date(y, m, d) + datetime.timedelta(days=delta)
            return (dt.year - 1900) * 10000 + dt.month * 100 + dt.day
        except (ValueError, OverflowError):
            return 0
    return 0

def pl_calctime(*args, **kwargs):
    """CalcTime(RefTime, MinChange): add/subtract minutes from a time (HHMM)."""
    if len(args) >= 2:
        try:
            ref = int(float(args[0]))
            delta = int(float(args[1]))
            hours = ref // 100
            minutes = ref % 100
            total_min = hours * 60 + minutes + delta
            # Wrap around 24h
            total_min = total_min % (24 * 60)
            if total_min < 0:
                total_min += 24 * 60
            return (total_min // 60) * 100 + (total_min % 60)
        except (ValueError, OverflowError):
            return 0
    return 0


# ---- Logical / Conditional Functions ----

def pl_iff(*args, **kwargs):
    """IFF(Condition, TrueVal, FalseVal): inline conditional."""
    if len(args) >= 3:
        return args[1] if args[0] else args[2]
    return 0

def pl_countif(*args, **kwargs):
    """CountIf(Test, Length): count of true conditions over last Length bars."""
    if len(args) >= 2 and hasattr(args[0], '__len__'):
        series = args[0]
        length = int(args[1])
        window = series[-length:] if len(series) >= length else series
        return sum(1 for v in window if v)
    return 0

def pl_countif_window(close_series, open_series, length):
    """CountIf(Close > Open, Length): count bars where close > open over window."""
    if not hasattr(close_series, '__len__') or not hasattr(open_series, '__len__'):
        return 0
    n = min(int(length), len(close_series), len(open_series))
    if n <= 0:
        return 0
    return sum(1 for i in range(-n, 0) if close_series[i] > open_series[i])


# ---- Statistical / List Functions ----

def pl_avglist(*args, **kwargs):
    """AvgList(V1, V2, ...): average of variadic args."""
    if args:
        nums = [float(a) for a in args if a is not None]
        return f32(sum(nums) / len(nums)) if nums else 0.0
    return 0.0

def pl_sumlist(*args, **kwargs):
    """SumList(V1, V2, ...): sum of variadic args."""
    if args:
        nums = [float(a) for a in args if a is not None]
        return f32(sum(nums)) if nums else 0.0
    return 0.0

def pl_maxlist2(*args, **kwargs):
    """MaxList2(V1, V2, ...): second highest of variadic args.
    PDF the PowerLanguage keyword reference:6981 — MaxList2(-5,0,12,7) == 7."""
    nums = sorted((float(a) for a in args if a is not None), reverse=True)
    if len(nums) >= 2:
        return f32(nums[1])
    return f32(nums[0]) if nums else 0.0

def pl_minlist2(*args, **kwargs):
    """MinList2(V1, V2, ...): second lowest of variadic args.
    PDF the PowerLanguage keyword reference:6999 — MinList2(-5,0,12,7) == 0."""
    nums = sorted(float(a) for a in args if a is not None)
    if len(nums) >= 2:
        return f32(nums[1])
    return f32(nums[0]) if nums else 0.0


# ---- Regression / Trend Indicators ----

def pl_linear_reg_angle(*args, **kwargs):
    """LinearRegAngle(Price, Length): regression angle in degrees.
    Skips leading padding zeros."""
    if len(args) >= 2 and hasattr(args[0], '__len__'):
        import math
        series = args[0]
        length = int(args[1])
        window, n = _valid_window(series, length)
        if n < length:
            return 0.0
        sum_x = n * (n - 1) / 2
        sum_y = sum(window)
        sum_xy = sum(i * window[i] for i in range(n))
        sum_x2 = n * (n - 1) * (2 * n - 1) / 6
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return 0.0
        slope = (n * sum_xy - sum_x * sum_y) / denom
        return f32(math.degrees(math.atan(slope)))
    return 0.0


def _tr_rma(series, length):
    """Wilder's RMA (rolling moving average) used in ADX."""
    if not series or length <= 0:
        return 0.0
    if len(series) < length:
        return sum(series) / len(series)
    # First value = SMA
    alpha = 1.0 / length
    rma = sum(series[-length:-length+1]) / length if length > 0 else 0.0  # rough start
    # Actually use the correct Wilder's: first value is SMA, then alpha * val + (1-alpha)*prev
    # For the whole series:
    rma = sum(series[:length]) / length
    for val in series[length:]:
        rma = val * alpha + rma * (1 - alpha)
    return rma

def _true_range_for_adx(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

def _find_first_valid(series):
    """Find index of first non-zero element in series."""
    for i, v in enumerate(series):
        if v != 0:
            return i
    return len(series)

def _adx_engine(high_s, low_s, close_s, length_val):
    """Incremental Wilder ADX engine. Carries the running smoothed TR/+DM/-DM and
    ADX state across bars (keyed by the series ids + length), extending by the new
    bars only instead of recomputing the whole history every call (the old O(n^2)).
    Reproduces the EXACT float op sequence of the former full recompute; reset
    between runs via reset_indicator_caches(). Returns the state dict, whose
    st['vals'][b] is the ADX value as of bar b (== old pl_adx(series[:b+1]))."""
    n = min(len(high_s), len(low_s), len(close_s))
    key = ('adxeng', id(high_s), id(low_s), id(close_s), length_val)
    st = _ind_cache.get(key)
    if st is None or st.get('n', 0) > n:
        st = {'start': None, 'next_i': None, 'm': 0,
              'seed_tr': 0.0, 'seed_dp': 0.0, 'seed_dm': 0.0,
              'tr_s': None, 'dp_s': None, 'dm_s': None,
              'dx_count': 0, 'adx_seed': 0.0, 'adx_val': None,
              'vals': [], 'n': 0}
        _ind_cache[key] = st
    if st['start'] is None:
        s = max(_find_first_valid(high_s), _find_first_valid(low_s), _find_first_valid(close_s))
        if s < n:
            st['start'] = s
            st['next_i'] = s + 1
    # NUMERIC MODEL: the Wilder smoothing runs in DOUBLE precision; only the per-bar
    # ADX result is rounded to f32 (see pl_rsi note / GT1 runsum). The former
    # f32-per-step model drifted past tolerance for the long-memory recursion at full
    # range (GT4 adx ~3.7e-5 by bar ~11.8k). Inputs are f32 (prices are f32-stored).
    alpha = 1.0 / length_val
    one_minus = 1.0 - alpha
    start = st['start']
    while len(st['vals']) < n:
        b = len(st['vals'])
        if start is not None and st['next_i'] == b and b < n:
            i = b
            h, l, pc = f32(high_s[i]), f32(low_s[i]), f32(close_s[i-1])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            up_move = f32(high_s[i]) - f32(high_s[i-1])
            down_move = f32(low_s[i-1]) - f32(low_s[i])
            dp = up_move if up_move > down_move and up_move > 0 else 0.0
            dm = down_move if down_move > up_move and down_move > 0 else 0.0
            j = st['m']  # 0-based index into the tr/dm series
            if j < length_val:
                st['seed_tr'] += tr
                st['seed_dp'] += dp
                st['seed_dm'] += dm
                if j == length_val - 1:
                    st['tr_s'] = st['seed_tr'] / length_val
                    st['dp_s'] = st['seed_dp'] / length_val
                    st['dm_s'] = st['seed_dm'] / length_val
            else:
                st['tr_s'] = tr * alpha + st['tr_s'] * one_minus
                st['dp_s'] = dp * alpha + st['dp_s'] * one_minus
                st['dm_s'] = dm * alpha + st['dm_s'] * one_minus
                di_sum = st['dp_s'] + st['dm_s']
                if di_sum > 0:
                    dx = abs(st['dp_s'] - st['dm_s']) / di_sum * 100.0
                    k = st['dx_count']
                    if k < length_val:
                        st['adx_seed'] += dx
                        st['dx_count'] = k + 1
                        if k == length_val - 1:
                            st['adx_val'] = st['adx_seed'] / length_val
                    else:
                        st['adx_val'] = dx * alpha + st['adx_val'] * one_minus
                        st['dx_count'] = k + 1
            st['m'] += 1
            st['next_i'] += 1
        # Record the ADX value as of bar b (matches old pl_adx return logic).
        if st['m'] < length_val:
            st['vals'].append(f32(0.0))
        elif st['dx_count'] < length_val:
            st['vals'].append(f32(50.0))
        else:
            st['vals'].append(f32(st['adx_val']))
    st['n'] = n
    return st

def pl_adx(*args, **kwargs):
    """ADX(high, low, close, Length): Average Directional Index.
    Uses f32 at every intermediate step for float32 fidelity.
    Incremental via _adx_engine; byte-identical to the former full recompute."""
    if len(args) >= 4:
        high_s, low_s, close_s, length_val = args[0], args[1], args[2], int(args[3])
        if not all(hasattr(s, '__len__') for s in (high_s, low_s, close_s)):
            return f32(0.0)
        if len(close_s) < length_val + 1:
            return f32(0.0)
        n = min(len(high_s), len(low_s), len(close_s))
        if n == 0:
            return f32(0.0)
        st = _adx_engine(high_s, low_s, close_s, length_val)
        return f32(st['vals'][n - 1])
    return f32(0.0)

def pl_adxr(*args, **kwargs):
    """ADXR(high, low, close, Length): Average Directional Movement Rating.
    ADXR = (ADX + ADX[Length-1]) / 2  (uses Length-1 bars ago, verified 2026-06-18).
    Reads the ADX value Length-1 bars ago from the shared incremental engine's
    per-bar history instead of recomputing pl_adx on a fresh sliced series each bar
    (which defeated the id()-keyed cache and was O(n^2))."""
    if len(args) >= 4:
        high_s, low_s, close_s, length_val = args[0], args[1], args[2], int(args[3])
        if not all(hasattr(s, '__len__') for s in (high_s, low_s, close_s)):
            return f32(0.0)
        n = min(len(high_s), len(low_s), len(close_s))
        if n == 0:
            return f32(0.0)
        # current_adx mirrors pl_adx's early guard (returns 0.0 when too few bars).
        if len(close_s) < length_val + 1:
            current_adx = f32(0.0)
        else:
            st = _adx_engine(high_s, low_s, close_s, length_val)
            current_adx = f32(st['vals'][n - 1])
        lag = length_val - 1  # ADX from Length-1 bars ago
        prior_adx = current_adx
        if lag > 0 and len(close_s) > lag:
            prior_n = n - lag  # == len(close_s[:-lag])
            if prior_n > 0:
                # old: pl_adx(series[:-lag]) == ADX as of bar (prior_n - 1).
                if prior_n < length_val + 1:
                    prior_adx = f32(0.0)
                else:
                    st = _adx_engine(high_s, low_s, close_s, length_val)
                    prior_adx = f32(st['vals'][prior_n - 1])
        return f32(f32(current_adx + prior_adx) / 2.0)
    return f32(0.0)

def _compute_adx_series(high_s, low_s, close_s, length):
    """Compute the full ADX series for a given data window."""
    import math
    if not all(hasattr(s, '__len__') for s in (high_s, low_s, close_s)):
        return []
    n = min(len(high_s), len(low_s), len(close_s))
    if n < length + 1:
        return []
    # Skip leading zeros
    start = max(_find_first_valid(high_s), _find_first_valid(low_s), _find_first_valid(close_s))
    if start >= n:
        return []
    tr_series = []
    dmplus_series = []
    dmminus_series = []
    for i in range(start + 1, n):
        h, l, pc = high_s[i], low_s[i], close_s[i-1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_series.append(tr)
        up_move = high_s[i] - high_s[i-1]
        down_move = low_s[i-1] - low_s[i]
        dmplus_series.append(max(up_move, 0) if up_move > down_move and up_move > 0 else 0.0)
        dmminus_series.append(max(down_move, 0) if down_move > up_move and down_move > 0 else 0.0)
    if len(tr_series) < length:
        return []
    alpha = 1.0 / length
    tr_s = sum(tr_series[:length]) / length
    dp_s = sum(dmplus_series[:length]) / length
    dm_s = sum(dmminus_series[:length]) / length
    adx_alpha = 1.0 / length
    dx_values = []
    # First compute all DX values
    for j in range(length, len(tr_series)):
        tr_s = tr_series[j] * alpha + tr_s * (1 - alpha)
        dp_s = dmplus_series[j] * alpha + dp_s * (1 - alpha)
        dm_s = dmminus_series[j] * alpha + dm_s * (1 - alpha)
        di_sum = dp_s + dm_s
        if di_sum > 0:
            dx_values.append(abs(dp_s - dm_s) / di_sum * 100.0)
        else:
            dx_values.append(0.0)
    # Then build ADX series using Wilder's smoothing of DX
    adx_values = []
    if len(dx_values) < length:
        return adx_values
    adx_val = sum(dx_values[:length]) / length
    adx_values.append(adx_val)
    for dx_val in dx_values[length:]:
        adx_val = dx_val * adx_alpha + adx_val * (1 - adx_alpha)
        adx_values.append(adx_val)
    return adx_values

def _calc_dmi(high, low, close, length, direction='plus'):
    """Helper to calculate DM+ or DM- for ADX family.
    Incremental Wilder smoothing keyed by (series ids, length, direction): carries
    the running smoothed TR/DM across bars instead of rebuilding the whole history
    every call (the old O(n^2)); reset per run via reset_indicator_caches().
    Byte-identical to the former full recompute. NOTE: the DM/TR here use RAW double
    arithmetic (no per-op f32) on purpose — this differs from pl_adx's f32 DM/TR, so
    the two engines are kept separate."""
    if not all(hasattr(s, '__len__') for s in (high, low, close)):
        return 0.0
    if len(high) < length + 1 or len(low) < length + 1 or len(close) < length + 1:
        return 0.0
    n = min(len(high), len(low), len(close))
    key = ('dmi', id(high), id(low), id(close), length, direction)
    st = _ind_cache.get(key)
    if st is None or st.get('n', 0) > n:
        st = {'start': None, 'next_i': None, 'm': 0,
              'seed_tr': 0.0, 'seed_dm': 0.0, 'tr_s': None, 'dm_s': None, 'n': 0}
        _ind_cache[key] = st
    if st['start'] is None:
        s = max(_find_first_valid(high), _find_first_valid(low), _find_first_valid(close))
        if s < n:
            st['start'] = s
            st['next_i'] = s + 1
        else:
            st['n'] = n
            return 0.0
    # NUMERIC MODEL: Wilder smoothing in DOUBLE precision, f32 only on the final
    # result (see pl_rsi / _adx_engine notes); inputs are f32 (prices f32-stored).
    alpha = 1.0 / length
    one_minus = 1.0 - alpha
    i = st['next_i']
    while i < n:
        h, l, pc = f32(high[i]), f32(low[i]), f32(close[i-1])
        tr = _true_range_for_adx(h, l, pc)
        up_move = f32(high[i]) - f32(high[i-1])
        down_move = f32(low[i-1]) - f32(low[i])
        dm_plus = up_move if (up_move > down_move and up_move > 0) else 0.0
        dm_minus = down_move if (down_move > up_move and down_move > 0) else 0.0
        dm = dm_plus if direction == 'plus' else dm_minus
        j = st['m']
        if j < length:
            st['seed_tr'] += tr
            st['seed_dm'] += dm
            if j == length - 1:
                st['tr_s'] = st['seed_tr'] / length
                st['dm_s'] = st['seed_dm'] / length
        else:
            st['tr_s'] = tr * alpha + st['tr_s'] * one_minus
            st['dm_s'] = dm * alpha + st['dm_s'] * one_minus
        st['m'] += 1
        i += 1
    st['next_i'] = i
    st['n'] = n
    m = st['m']
    if m == 0:
        return f32(0.0)
    if m < length:
        tr_smooth = st['seed_tr'] / m
        dm_smooth = st['seed_dm'] / m
    else:
        tr_smooth = st['tr_s']
        dm_smooth = st['dm_s']
    if tr_smooth == 0:
        return f32(0.0)
    return f32(dm_smooth / tr_smooth * 100.0)

def pl_dmiplus(*args, **kwargs):
    """DMIPlus(high, low, close, Length): +DI value."""
    if len(args) >= 4:
        return f32(_calc_dmi(args[0], args[1], args[2], int(args[3]), 'plus'))
    return f32(0.0)

def pl_dmiminus(*args, **kwargs):
    """DMIMinus(high, low, close, Length): -DI value."""
    if len(args) >= 4:
        return f32(_calc_dmi(args[0], args[1], args[2], int(args[3]), 'minus'))
    return f32(0.0)


def pl_bollinger_band(*args, **kwargs):
    """BollingerBand(Price, Length, NumDevs): upper (+devs) or lower (-devs) band.
    Skips leading padding zeros."""
    if len(args) >= 3 and hasattr(args[0], '__len__'):
        import math
        series = args[0]
        length = int(args[1])
        num_devs = float(args[2])
        window, n = _valid_window(series, length)
        if n < length:
            return 0.0
        mean = f32(sum(window) / length)
        variance = f32(sum(f32((x - mean) ** 2) for x in window) / length)
        std = f32(math.sqrt(variance))
        return f32(mean + num_devs * std)
    return 0.0


def pl_stochastic(*args, **kwargs):
    """Stochastic(High, Low, Close, Length, SmoothK, SmoothD, SmoothType, oFastK, oFastD, oSlowK, oSlowD)
    Multi-output var-parameter function. Returns (status, FastK, FastD, SlowK, SlowD) tuple.
    Output params (oFastK, oFastD, oSlowK, oSlowD) are returned as tuple elements.
    Uses f32 at every intermediate step for float32 fidelity.
    Computes with partial data when fewer than `length` bars are available.
    Incremental: the Fast %K series is cached per (series ids, length) and extended
    by the new bar only (the old recompute rebuilt the whole %K/%SlowK history every
    call -> O(n^2)); the %D/SlowK/SlowD readouts derive from the cached tail. Reset
    per run via reset_indicator_caches(). Byte-identical to the former recompute."""
    if len(args) >= 7:
        high_s = args[0]
        low_s = args[1]
        close_s = args[2]
        length = int(args[3])
        smoothk = int(args[4])
        smoothd = int(args[5])
        smoothtype = int(args[6])  # 1=SMA, 2=EMA, etc. (currently only SMA implemented)

        if not all(hasattr(s, '__len__') for s in (high_s, low_s, close_s)):
            return (1, f32(0.0), f32(0.0), f32(0.0), f32(0.0))

        # Skip leading padding zeros in all three series
        start = max(_find_first_valid(high_s), _find_first_valid(low_s), _find_first_valid(close_s))

        if start >= len(close_s):
            return (1, f32(50.0), f32(50.0), f32(50.0), f32(50.0))

        N = min(len(high_s), len(low_s), len(close_s))
        key = ('stoch', id(high_s), id(low_s), id(close_s), length)
        st = _ind_cache.get(key)
        if st is None or st.get('cn', 0) > N:
            st = {'start': start, 'next': start, 'fastk': [], 'cn': 0}
            _ind_cache[key] = st
        fk = st['fastk']
        i = st['next']
        while i < N:
            vi = i - start  # index within the valid (post-padding) data
            lookback = min(length, vi + 1)
            hh = f32(max(high_s[i + 1 - lookback:i + 1]))
            ll = f32(min(low_s[i + 1 - lookback:i + 1]))
            if hh == ll:
                fk.append(f32(50.0))
            else:
                fk.append(f32(f32(f32(close_s[i] - ll) / f32(hh - ll)) * 100.0))
            i += 1
        st['next'] = i
        st['cn'] = N

        n = len(fk)  # == valid bar count (matches the old `n`)
        if n < 1:
            return (1, f32(50.0), f32(50.0), f32(50.0), f32(50.0))

        current_fastk = fk[-1]

        # Fast %D = SMA of Fast %K over smoothk periods (1st smoothing)
        if n >= smoothk:
            fastd = f32(sum(fk[-smoothk:]) / smoothk)
        else:
            fastd = f32(sum(fk) / n)

        # Slow %K = same as Fast %D (EL: "SlowK is equal to FastD")
        slowk = fastd

        # Slow %D = SMA of the SlowK series over smoothd periods (2nd smoothing).
        # SlowK[vi] = SMA of Fast %K over the last min(smoothk, vi+1) bars; we only
        # need the last smoothd of them (or all when fewer exist).
        def _slowk_at(vi):
            lb = min(smoothk, vi + 1)
            return f32(sum(fk[vi + 1 - lb:vi + 1]) / lb)

        if n >= smoothd:
            slowd = f32(sum(_slowk_at(vi) for vi in range(n - smoothd, n)) / smoothd)
        else:
            slowd = f32(sum(_slowk_at(vi) for vi in range(n)) / n)

        return (1, f32(current_fastk), f32(fastd), f32(slowk), f32(slowd))
    return (1, f32(0.0), f32(0.0), f32(0.0), f32(0.0))


# ---- Price / Volume Reserved Words as Functions ----

def pl_ticks(*args, **kwargs):
    """Ticks: current bar tick count."""
    return kwargs.get('ticks', 0)

def pl_upticks(*args, **kwargs):
    """UpTicks: current bar uptick count."""
    return kwargs.get('upticks', 0)

def pl_downticks(*args, **kwargs):
    """DownTicks: current bar downtick count."""
    return kwargs.get('downticks', 0)

def pl_openint(*args, **kwargs):
    """OpenInt: open interest."""
    return kwargs.get('openint', 0)


# ---- Daily Session Data Functions ----

def pl_close_d(*args, **kwargs):
    """CloseD(PeriodsAgo): daily close."""
    return kwargs.get('closed', 0)

def pl_open_d(*args, **kwargs):
    """OpenD(PeriodsAgo): daily open."""
    return kwargs.get('opend', 0)

def pl_high_d(*args, **kwargs):
    """HighD(PeriodsAgo): daily high."""
    return kwargs.get('highd', 0)

def pl_low_d(*args, **kwargs):
    """LowD(PeriodsAgo): daily low."""
    return kwargs.get('lowd', 0)


# ============================================================
# Auto-generated runtime stubs for all PL built-in functions
# ============================================================

def pl_allowsendordersalways(*args, **kwargs):
    return 0

def pl_arctangent(*args, **kwargs):
    import math
    if args:
        return f32(math.degrees(math.atan(float(args[0]))))
    return 0.0

def pl_array_compare(*args, **kwargs):
    return 0

def pl_array_contains(*args, **kwargs):
    """EL Array_Contains(arr, value): True/False whether value is in the array
    (pdf:6034-6036)."""
    if len(args) >= 2 and isinstance(args[0], list):
        return args[1] in args[0]
    return False

def pl_array_copy(*args, **kwargs):
    return 0

def _pl_array_getval(args, default):
    """EL Array_GetXxxValue(arr, index): read arr[index]. The transpiler emits
    arrays as real Python lists, so honour them; tolerate bad/oob access by
    returning the type default (prevents an exception that would abort the bar
    and zero the whole trace)."""
    if len(args) >= 2 and isinstance(args[0], list):
        try:
            return args[0][int(args[1])]
        except (IndexError, ValueError, TypeError):
            return default
    return default

def _pl_array_setval(args, default):
    """EL Array_SetXxxValue(arr, index, value): write value into arr[index] when
    arr is a real list. Returns the written value (EL returns 1 on success but
    callers ignore the return)."""
    if len(args) >= 3 and isinstance(args[0], list):
        try:
            args[0][int(args[1])] = args[2]
            return args[2]
        except (IndexError, ValueError, TypeError):
            return default
    return default

def pl_array_getbooleanvalue(*args, **kwargs):
    return _pl_array_getval(args, False)

def pl_array_getfloatvalue(*args, **kwargs):
    return _pl_array_getval(args, 0.0)

def pl_array_getintegervalue(*args, **kwargs):
    return _pl_array_getval(args, 0)

def pl_array_getstringvalue(*args, **kwargs):
    return _pl_array_getval(args, '')

def pl_array_gettype(*args, **kwargs):
    """EL Array_GetType(arr): 2=true/false array, 3=string array,
    7=double-precision numerical array (pdf:6131-6133)."""
    arr = args[0] if args else None
    if isinstance(arr, list):
        if any(isinstance(x, str) for x in arr):
            return 3
        if arr and all(isinstance(x, bool) for x in arr):
            return 2
        return 7
    return 7

def pl_array_indexof(*args, **kwargs):
    """EL Array_IndexOf(arr, value): index of first matching element, -1 if not
    contained (pdf:6139-6140)."""
    if len(args) >= 2 and isinstance(args[0], list):
        try:
            return args[0].index(args[1])
        except ValueError:
            return -1
    return -1

def pl_array_setbooleanvalue(*args, **kwargs):
    return _pl_array_setval(args, False)

def pl_array_setfloatvalue(*args, **kwargs):
    return _pl_array_setval(args, 0.0)

def pl_array_setintegervalue(*args, **kwargs):
    return _pl_array_setval(args, 0)

def pl_array_setstringvalue(*args, **kwargs):
    return _pl_array_setval(args, '')

def pl_array_setvalrange(*args, **kwargs):
    return 0

def pl_array_sort(*args, **kwargs):
    return 0

def pl_arraysize(*args, **kwargs):
    return 0

def pl_arraystartaddr(*args, **kwargs):
    return 0

def pl_arw_anchor_to_bars(*args, **kwargs):
    return 0

def pl_arw_delete(*args, **kwargs):
    return 0

def pl_arw_get_anchor_to_bars(*args, **kwargs):
    return 0

def pl_arw_getactive(*args, **kwargs):
    return 0

def pl_arw_getbarnumber(*args, **kwargs):
    return 0

def pl_arw_getcolor(*args, **kwargs):
    return 0

def pl_arw_getdate(*args, **kwargs):
    return 0

def pl_arw_getdirection(*args, **kwargs):
    return 0

def pl_arw_getfirst(*args, **kwargs):
    return 0

def pl_arw_getlock(*args, **kwargs):
    return 0

def pl_arw_getnext(*args, **kwargs):
    return 0

def pl_arw_getsize(*args, **kwargs):
    return 0

def pl_arw_getstyle(*args, **kwargs):
    return 0

def pl_arw_gettext(*args, **kwargs):
    return 0

def pl_arw_gettextattribute(*args, **kwargs):
    return 0

def pl_arw_gettextbgcolor(*args, **kwargs):
    return 0

def pl_arw_gettextcolor(*args, **kwargs):
    return 0

def pl_arw_gettextfontname(*args, **kwargs):
    return 0

def pl_arw_gettextsize(*args, **kwargs):
    return 0

def pl_arw_gettime(*args, **kwargs):
    return 0

def pl_arw_gettime_dt(*args, **kwargs):
    return 0

def pl_arw_gettime_s(*args, **kwargs):
    return 0

def pl_arw_getval(*args, **kwargs):
    return 0

def pl_arw_lock(*args, **kwargs):
    return 0

def pl_arw_new_bn(*args, **kwargs):
    return 0

def pl_arw_new_dt(*args, **kwargs):
    return 0

def pl_arw_setbarnumber(*args, **kwargs):
    return 0

def pl_arw_setcolor(*args, **kwargs):
    return 0

def pl_arw_setlocation(*args, **kwargs):
    return 0

def pl_arw_setlocation_bn(*args, **kwargs):
    return 0

def pl_arw_setlocation_dt(*args, **kwargs):
    return 0

def pl_arw_setlocation_s(*args, **kwargs):
    return 0

def pl_arw_setsize(*args, **kwargs):
    return 0

def pl_arw_setstyle(*args, **kwargs):
    return 0

def pl_arw_settext(*args, **kwargs):
    return 0

def pl_arw_settextattribute(*args, **kwargs):
    return 0

def pl_arw_settextbgcolor(*args, **kwargs):
    return 0

def pl_arw_settextcolor(*args, **kwargs):
    return 0

def pl_arw_settextfontname(*args, **kwargs):
    return 0

def pl_arw_settextsize(*args, **kwargs):
    return 0

def pl_asksize(*args, **kwargs):
    return 0

def pl_atcommentarybar(*args, **kwargs):
    return 0

def pl_autosession(*args, **kwargs):
    return 0

def pl_bidsize(*args, **kwargs):
    return 0

def pl_bigpointvalue(*args, **kwargs):
    return 0

def pl_boxsize(*args, **kwargs):
    return 0

def pl_changemarketposition(*args, **kwargs):
    return 0

def pl_checkcommentary(*args, **kwargs):
    return 0

def pl_cleardebug(*args, **kwargs):
    return 0

def pl_clearprintlog(*args, **kwargs):
    return 0

def pl_commandline(*args, **kwargs):
    return 0

def pl_commentarycl(*args, **kwargs):
    return 0

def pl_commentaryenabled(*args, **kwargs):
    return 0

def pl_commission(*args, **kwargs):
    return 0

def pl_computerdatetime(*args, **kwargs):
    return 0

def pl_convert_currency(*args, **kwargs):
    return 0

def pl_cosine(*args, **kwargs):
    import math
    if args:
        return f32(math.cos(math.radians(float(args[0]))))
    return 0.0

def pl_cotangent(*args, **kwargs):
    # EL trig functions take DEGREES (cf. pl_tangent/pl_sine using math.radians).
    # Cotangent = 1/tan; Cotangent(45 deg) = 1.0 (pdf: Cotangent math fn).
    import math
    if args:
        return f32(1.0 / math.tan(math.radians(float(args[0]))))
    return 0.0

def pl_currentopenint(*args, **kwargs):
    return 0

def pl_currenttime_s(*args, **kwargs):
    return 0

def pl_currentdate_s(*args, **kwargs):
    return 0

def pl_category(*args, **kwargs):
    """Category: instrument/symbol category code (chart config). Stub: 0."""
    return 0

def pl_description(*args, **kwargs):
    """Description: instrument description string (unimplemented; loud-stub -> '')."""
    return 0

def pl_intervaltype(*args, **kwargs):
    """IntervalType: data-series resolution interval code (chart config).
    This NQ chart is 1-minute intra-day data => 1 (Intra-Day: Seconds/Minutes/
    Hours), per pdf:3542. Constant chart config (cf. session-config constants)."""
    return 1

def pl_intervaltype_ex(*args, **kwargs):
    """IntervalType_ex: extended resolution interval code (chart config).
    1-minute bars => 2 (Minutes), per pdf:3566."""
    return 2

def pl_dailyclose(*args, **kwargs):
    return 0

def pl_dailyhigh(*args, **kwargs):
    return 0

def pl_dailylimit(*args, **kwargs):
    return 0

def pl_dailylow(*args, **kwargs):
    return 0

def pl_dailyopen(*args, **kwargs):
    return 0

def pl_dailyvolume(*args, **kwargs):
    return 0

def pl_datacompression(*args, **kwargs):
    return 0

def pl_datetime(*args, **kwargs):
    return 0

# --- DateTime helpers (GTA6 date/time conversion family) --------------------
# PowerLanguage uses two grounded numeric encodings here:
#   * "DateTime serial": int part = days since 1899-12-30 (OLE/Excel style),
#     frac = fraction of day. EncodeDate/StringToDate/IncMonth use this
#     (pdf:4558 EncodeDate(08,01,01)=39448; pdf:4704 IncMonth(39417,1)=39448).
#   * The runner's per-bar `datetime` value is YYYYMMDD.frac (run_gt builds it
#     from Date+Time). DateTime2ELTime/DayOfWeek/*ToString/FormatDate/FormatTime
#     consume that form. Both share the same fractional-day convention.
def _ole_from_ymd(yyyy, mm, dd):
    import datetime as _dt
    return (_dt.date(int(yyyy), int(mm), int(dd)) - _dt.date(1899, 12, 30)).days

def _ymd_from_ole(serial):
    import datetime as _dt
    d = _dt.date(1899, 12, 30) + _dt.timedelta(days=int(round(float(serial))))
    return d.year, d.month, d.day

def _decode_dt_yyyymmdd(dt):
    """Decode the runner's YYYYMMDD.frac bar DateTime into (y,mo,d,h,mi,s)."""
    dt = float(dt)
    ip = int(dt)
    frac = dt - ip
    tot = int(round(frac * 86400.0))
    return ip // 10000, (ip // 100) % 100, ip % 100, tot // 3600, (tot % 3600) // 60, tot % 60

def _fmt_dt(fmt, y, mo, d, h, mi, s, ms):
    import re as _re, calendar as _cal
    h12 = h % 12 or 12
    tt = 'AM' if h < 12 else 'PM'
    wd = _cal.weekday(y, mo, d)  # Mon=0..Sun=6
    full = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    tokens = {
        'dddd': full[wd], 'ddd': full[wd][:3], 'dd': f"{d:02d}", 'd': str(d),
        'MMMM': _cal.month_name[mo], 'MMM': _cal.month_abbr[mo],
        'MM': f"{mo:02d}", 'M': str(mo),
        'yyyy': f"{y:04d}", 'yy': f"{y % 100:02d}", 'y': str(y % 100),
        'HH': f"{h:02d}", 'H': str(h), 'hh': f"{h12:02d}", 'h': str(h12),
        'mm': f"{mi:02d}", 'm': str(mi), 'ss': f"{s:02d}", 's': str(s),
        'fff': f"{ms:03d}", 'tt': tt, 't': tt[0],
    }
    pat = _re.compile('|'.join(sorted((_re.escape(k) for k in tokens), key=len, reverse=True)))
    return pat.sub(lambda m: tokens[m.group(0)], fmt)

def _parse_time_frac(s):
    """Parse 'hh:mm:ss tt' (or 24h) -> fraction of day."""
    import re as _re
    s = str(s).strip()
    ampm = None
    m = _re.search(r'(?i)\b(AM|PM)\b', s)
    if m:
        ampm = m.group(1).upper()
        s = s[:m.start()].strip()
    parts = [int(x) for x in s.split(':') if x != '']
    hh = parts[0] if parts else 0
    mi = parts[1] if len(parts) > 1 else 0
    ss = parts[2] if len(parts) > 2 else 0
    if ampm == 'PM' and hh < 12:
        hh += 12
    if ampm == 'AM' and hh == 12:
        hh = 0
    return (hh * 3600 + mi * 60 + ss) / 86400.0

def _parse_dt_formatted(value, fmt):
    """StringToDTFormatted: parse `value` using a MC format string -> serial(+frac)."""
    import re as _re
    field_pat = {
        'yyyy': (r'\d{4}', 'y4'), 'yy': (r'\d{2}', 'y2'),
        'MM': (r'\d{1,2}', 'M'), 'M': (r'\d{1,2}', 'M'),
        'dd': (r'\d{1,2}', 'd'), 'd': (r'\d{1,2}', 'd'),
        'HH': (r'\d{1,2}', 'H'), 'H': (r'\d{1,2}', 'H'),
        'hh': (r'\d{1,2}', 'h'), 'h': (r'\d{1,2}', 'h'),
        'mm': (r'\d{1,2}', 'mi'), 'm': (r'\d{1,2}', 'mi'),
        'ss': (r'\d{1,2}', 's'), 's': (r'\d{1,2}', 's'),
        'tt': (r'[APap][Mm]', 'ap'), 't': (r'[APap]', 'ap'),
    }
    toks = sorted(field_pat, key=len, reverse=True)
    regex, fields, i = '', [], 0
    while i < len(fmt):
        for t in toks:
            if fmt.startswith(t, i):
                pat, field = field_pat[t]
                regex += '(' + pat + ')'
                fields.append(field)
                i += len(t)
                break
        else:
            regex += _re.escape(fmt[i])
            i += 1
    m = _re.match(regex, str(value).strip())
    if not m:
        return 0
    vals = {f: m.group(idx + 1) for idx, f in enumerate(fields)}
    if 'y4' in vals:
        y = int(vals['y4'])
    elif 'y2' in vals:
        y = int(vals['y2'])
        y = 1900 + y if y >= 50 else 2000 + y
    else:
        y = 1900
    mo = int(vals.get('M', 1))
    d = int(vals.get('d', 1))
    h = int(vals.get('H', vals.get('h', 0)))
    mi = int(vals.get('mi', 0))
    s = int(vals.get('s', 0))
    ap = vals.get('ap', '').upper()
    if ap.startswith('P') and h < 12:
        h += 12
    if ap.startswith('A') and h == 12:
        h = 0
    serial = _ole_from_ymd(y, mo, d)
    frac = (h * 3600 + mi * 60 + s) / 86400.0
    return serial + frac if frac else float(serial)

def pl_datetime2eltime(*args, **kwargs):
    # DateTime2ELTime(DateTime) -> HHmm (pdf:4327 39449.65625 -> 1545).
    if not args:
        return 0
    _y, _mo, _d, h, mi, _s = _decode_dt_yyyymmdd(args[0])
    return h * 100 + mi

def pl_datetime2eltime_s(*args, **kwargs):
    # DateTime2ELTime_s(DateTime) -> HHmmss (pdf:4342 39449.646354167 -> 153045).
    if not args:
        return 0
    _y, _mo, _d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return h * 10000 + mi * 100 + s

def pl_datetime_bar_update(*args, **kwargs):
    return 0

def pl_datetimetostring(*args, **kwargs):
    # DateTimeToString(DateTime) -> regional date+time string (pdf:4355 notes the
    # format is Regional-Options controlled). Capture locale is d/MM/yyyy h:mm:ss
    # tt; capture row1 = '2/01/2024 9:21:00 AM'.
    if not args:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('d/MM/yyyy h:mm:ss tt', y, mo, d, h, mi, s, 0)

def pl_datetimetostring_ms(*args, **kwargs):
    # DateTimeToString_Ms(DateTime) -> regional date+time with ms (pdf:4368).
    # Capture row1 = '2/01/2024 09:21:00.000'.
    if not args:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('d/MM/yyyy HH:mm:ss.fff', y, mo, d, h, mi, s, 0)

def pl_datetostring(*args, **kwargs):
    # DateToString(DateTime) -> regional date string (pdf:4402; Regional-Options
    # controlled). Capture locale d/MM/yyyy; capture row1 = '2/01/2024'.
    if not args:
        return ''
    y, mo, d, _h, _mi, _s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('d/MM/yyyy', y, mo, d, 0, 0, 0, 0)

def pl_timetostring(*args, **kwargs):
    # TimeToString(DateTime) -> regional time string (pdf:5059; Regional-Options
    # controlled). Capture locale h:mm:ss tt; capture row1 = '9:21:00 AM'.
    if not args:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('h:mm:ss tt', y, mo, d, h, mi, s, 0)

def pl_datetojulian(*args, **kwargs):
    if not args:
        return 0
    el_date = int(args[0])
    yy = el_date // 10000
    mm = (el_date // 100) % 100
    dd = el_date % 100
    yyyy = 1900 + yy
    a = (14 - mm) // 12
    y = yyyy + 4800 - a
    m = mm + 12 * a - 3
    jdn = dd + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    # MC DateToJulian returns days since Dec 30, 1899 (Excel-style serial date)
    return jdn - 2415019

def pl_dayfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    date_part = int(dt)  # YYYYMMDD
    return date_part % 100  # DD

def pl_dayofweekfromdatetime(*args, **kwargs):
    # DayOfWeekFromDateTime(DateTime) -> Sunday=0..Saturday=6 (pdf:4465 Jan 1
    # 2008 -> 2, Tuesday).
    if not args:
        return 0
    import calendar as _cal
    y, mo, d, _h, _mi, _s = _decode_dt_yyyymmdd(args[0])
    return (_cal.weekday(y, mo, d) + 1) % 7

def pl_dom_askprice(*args, **kwargs):
    return 0

def pl_dom_askscount(*args, **kwargs):
    return 0

def pl_dom_asksize(*args, **kwargs):
    return 0

def pl_dom_bidprice(*args, **kwargs):
    return 0

def pl_dom_bidscount(*args, **kwargs):
    return 0

def pl_dom_bidsize(*args, **kwargs):
    return 0

def pl_dom_isconnected(*args, **kwargs):
    return 0

def pl_encodedate(*args, **kwargs):
    # EncodeDate(yy,MM,dd) -> DateTime serial, days since 1899-12-30 (pdf:4558
    # EncodeDate(08,01,01)=39448, Jan 1 2008; two-digit year).
    if len(args) < 3:
        return 0
    yy, mm, dd = int(args[0]), int(args[1]), int(args[2])
    if yy < 100:
        yy = 1900 + yy if yy >= 50 else 2000 + yy
    return float(_ole_from_ymd(yy, mm, dd))

def pl_encodetime(*args, **kwargs):
    # EncodeTime(HH,mm,ss,mmm) -> fraction of day (pdf:4574
    # EncodeTime(16,29,55,500)=0.6874479167).
    a = list(args) + [0, 0, 0, 0]
    hh, mm, ss, ms = int(a[0]), int(a[1]), int(a[2]), int(a[3])
    return (hh * 3600 + mm * 60 + ss + ms / 1000.0) / 86400.0

def pl_exchlisted(*args, **kwargs):
    return 0

def pl_execoffset(*args, **kwargs):
    return 0

def pl_expirationdate(*args, **kwargs):
    return 0

def pl_expirationdatefromvendor(*args, **kwargs):
    return 0

def pl_fileappend(*args, **kwargs):
    return 0

def pl_filedelete(*args, **kwargs):
    return 0

def pl_fill_array(*args, **kwargs):
    return 0

def pl_formatdate(*args, **kwargs):
    # FormatDate("FormatString", DateTime) -> formatted date string (pdf:4593).
    if len(args) < 2:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[1])
    return _fmt_dt(str(args[0]), y, mo, d, h, mi, s, 0)

def pl_formattime(*args, **kwargs):
    # FormatTime("FormatString", DateTime) -> formatted time string (pdf:4644).
    if len(args) < 2:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[1])
    return _fmt_dt(str(args[0]), y, mo, d, h, mi, s, 0)

def pl_getaccount(*args, **kwargs):
    return 0

def pl_getaccountid(*args, **kwargs):
    return 0

def pl_getappinfo(*args, **kwargs):
    return 0

def pl_getbackgroundcolor(*args, **kwargs):
    return 0

def pl_getbvalue(*args, **kwargs):
    return 0

def pl_getcdromdrive(*args, **kwargs):
    return 0

def pl_getcountry(*args, **kwargs):
    return 0

def pl_getcurrency(*args, **kwargs):
    return 0

def pl_getexchangename(*args, **kwargs):
    return 0

def pl_getgvalue(*args, **kwargs):
    return 0

def pl_getnumaccounts(*args, **kwargs):
    return 0

def pl_getnumpositions(*args, **kwargs):
    return 0

def pl_getplotbgcolor(*args, **kwargs):
    return 0

def pl_getplotcolor(*args, **kwargs):
    return 0

def pl_getplotwidth(*args, **kwargs):
    return 0

def pl_getpositionaverageprice(*args, **kwargs):
    return 0

def pl_getpositionopenpl(*args, **kwargs):
    return 0

def pl_getpositionquantity(*args, **kwargs):
    return 0

def pl_getpositionsymbol(*args, **kwargs):
    return 0

def pl_getpositiontotalcost(*args, **kwargs):
    return 0

def pl_getrtaccountequity(*args, **kwargs):
    return 0

def pl_getrtaccountnetworth(*args, **kwargs):
    return 0

def pl_getrtsymbolname(*args, **kwargs):
    return 0

def pl_getrtunrealizedpl(*args, **kwargs):
    return 0

def pl_getrvalue(*args, **kwargs):
    return 0

def pl_getstrategyname(*args, **kwargs):
    return 0

def pl_getsymbolname(*args, **kwargs):
    return 0

def pl_getuserid(*args, **kwargs):
    return 0

def pl_getusername(*args, **kwargs):
    return 0

def pl_gradientcolor(*args, **kwargs):
    return 0

def pl_hoursfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    total_seconds = int(round(frac * 86400))
    hours = total_seconds // 3600
    return hours

def pl_i_closedequity(*args, **kwargs):
    return 0

def pl_i_currentcontracts(*args, **kwargs):
    return 0

def pl_i_currentshares(*args, **kwargs):
    return 0

def pl_i_getplotvalue(*args, **kwargs):
    return 0

def pl_i_openequity(*args, **kwargs):
    return 0

def pl_i_setplotvalue(*args, **kwargs):
    return 0

def pl_incmonth(*args, **kwargs):
    # IncMonth(JulianDate, M): add M calendar months to a DateTime serial
    # (pdf:4704 IncMonth(39417,1)=39448). MultiCharts does NOT clamp the day to
    # the target month's length: it keeps the day-of-month and lets an overflow
    # roll forward into the following month. Verified against the GTA6 capture:
    #   IncMonth(Jan 30 2024, +1) -> "Feb 30" -> Mar 1 2024 (Feb has 29 days)
    #   IncMonth(Jan 31 2024, +1) -> "Feb 31" -> Mar 2 2024
    # i.e. days past the target month-end spill into the next month rather than
    # being truncated to the last valid day.
    if len(args) < 2:
        return 0
    import datetime as _dt, calendar as _cal
    y, mo, d = _ymd_from_ole(args[0])
    total = y * 12 + (mo - 1) + int(args[1])
    ny, nmo = total // 12, total % 12 + 1
    month_len = _cal.monthrange(ny, nmo)[1]
    base = _dt.date(ny, nmo, min(d, month_len))
    overflow = d - month_len if d > month_len else 0
    return (base + _dt.timedelta(days=overflow) - _dt.date(1899, 12, 30)).days

def pl_initialcapital(*args, **kwargs):
    return 0

def pl_insideask(*args, **kwargs):
    return 0

def pl_insidebid(*args, **kwargs):
    return 0

def pl_instr(*args, **kwargs):
    if len(args) >= 2:
        try:
            # InStr(String1, String2): 1-based position of String2 in String1
            return args[0].find(args[1]) + 1
        except (ValueError, AttributeError):
            return 0
    return 0

def pl_intrabarordergeneration(*args, **kwargs):
    return 0

def pl_jpy(*args, **kwargs):
    return 0

def pl_juliantodate(*args, **kwargs):
    if not args:
        return 0
    mc_jd = int(args[0])
    jdn = mc_jd + 2415019
    f = jdn + 1401 + (((4 * jdn + 274277) // 146097) * 3) // 4 - 38
    e = 4 * f + 3
    g = (e % 1461) // 4
    h = 5 * g + 2
    dd = (h % 153) // 5 + 1
    mm = (h // 153 + 2) % 12 + 1
    yy = e // 1461 - 4716 + (14 - mm) // 12
    el_date = ((yy - 1900) * 10000) + (mm * 100) + dd
    return el_date

def pl_last(*args, **kwargs):
    return 0

def pl_lastcalcdatetime(*args, **kwargs):
    return 0

def pl_lastcalcjdate(*args, **kwargs):
    return 0

def pl_lastcalcmmtime(*args, **kwargs):
    return 0

def pl_lastcalcmstime(*args, **kwargs):
    return 0

def pl_lastcalcsstime(*args, **kwargs):
    return 0

def pl_legacycolortorgb(*args, **kwargs):
    return 0

def pl_legacycolorvalue(*args, **kwargs):
    return 0

def pl_lower(*args, **kwargs):
    return 0

def pl_lpbool(*args, **kwargs):
    return 0

def pl_lpbyte(*args, **kwargs):
    return 0

def pl_lpdouble(*args, **kwargs):
    return 0

def pl_lpdword(*args, **kwargs):
    return 0

def pl_lpfloat(*args, **kwargs):
    return 0

def pl_lpint(*args, **kwargs):
    return 0

def pl_lplong(*args, **kwargs):
    return 0

def pl_lpstr(*args, **kwargs):
    return 0

def pl_lpword(*args, **kwargs):
    return 0

def pl_margin(*args, **kwargs):
    return 0

def pl_maxbarsback(*args, **kwargs):
    # MaxBarsBack: the study's "Maximum Bars Back" setting (pdf:6483). Fixed chart/
    # study config for the GTA6 capture = 50 (cf. session-config constants).
    return 50

def pl_maxbarsforward(*args, **kwargs):
    # MaxBarsForward: chart right-margin size in bars (pdf:6496). Fixed chart config
    # for the GTA6 capture = 16.
    return 16

def pl_maxcontractsheld(*args, **kwargs):
    return 0

def pl_maxiddrawdown(*args, **kwargs):
    return 0

def pl_maxshares(*args, **kwargs):
    return 0

def pl_maxsharesheld(*args, **kwargs):
    return 0

def pl_mc_arw_getactive(*args, **kwargs):
    return 0

def pl_mc_text_getactive(*args, **kwargs):
    return 0

def pl_mc_tl_getactive(*args, **kwargs):
    return 0

def pl_mc_tl_new(*args, **kwargs):
    return 0

def pl_mc_tl_new_bn(*args, **kwargs):
    return 0

def pl_mc_tl_new_dt(*args, **kwargs):
    return 0

def pl_mc_tl_new_self(*args, **kwargs):
    return 0

def pl_mc_tl_new_self_bn(*args, **kwargs):
    return 0

def pl_messagelog(*args, **kwargs):
    return 0

def pl_millisecondsfromdatetime(*args, **kwargs):
    return 0

def pl_minmove(*args, **kwargs):
    return 0

def pl_minutesfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    total_seconds = int(round(frac * 86400))
    hours = total_seconds // 3600
    minutes = (total_seconds - hours * 3600) // 60
    return minutes

def pl_monthfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    date_part = int(dt)  # YYYYMMDD
    return (date_part // 100) % 100  # MM

def pl_mouseclickbarnumber(*args, **kwargs):
    return 0

def pl_mouseclickctrlpressed(*args, **kwargs):
    return 0

def pl_mouseclickdatanumber(*args, **kwargs):
    return 0

def pl_mouseclickdatetime(*args, **kwargs):
    return 0

def pl_mouseclickprice(*args, **kwargs):
    return 0

def pl_mouseclickshiftpressed(*args, **kwargs):
    return 0

def pl_nthmaxlist(*args, **kwargs):
    if len(args) < 2:
        return 0
    n = int(args[0])
    values = list(args[1:])
    if n < 1 or n > len(values):
        return 0
    sorted_vals = sorted(values, reverse=True)
    return sorted_vals[n - 1]

def pl_nthminlist(*args, **kwargs):
    if len(args) < 2:
        return 0
    n = int(args[0])
    values = list(args[1:])
    if n < 1 or n > len(values):
        return 0
    sorted_vals = sorted(values)
    return sorted_vals[n - 1]

def pl_openentriescount(*args, **kwargs):
    return 0

def pl_optiontype(*args, **kwargs):
    return 0

def pl_placemarketorder(*args, **kwargs):
    return 0

def pl_playsound(*args, **kwargs):
    return 0

def pl_plotpaintbar(*args, **kwargs):
    return 0

def pl_pmm_get_global_named_num(*args, **kwargs):
    return 0

def pl_pmm_get_global_named_str(*args, **kwargs):
    return 0

def pl_pmm_get_my_named_num(*args, **kwargs):
    return 0

def pl_pmm_get_my_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_global_named_num(*args, **kwargs):
    return 0

def pl_pmm_set_global_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_my_named_num(*args, **kwargs):
    return 0

def pl_pmm_set_my_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_my_status(*args, **kwargs):
    return 0

def pl_pmms_get_strategy_named_num(*args, **kwargs):
    return 0

def pl_pmms_get_strategy_named_str(*args, **kwargs):
    return 0

def pl_pmms_set_strategy_named_num(*args, **kwargs):
    return 0

def pl_pmms_set_strategy_named_str(*args, **kwargs):
    return 0

def pl_pmms_strategies_allow_entries_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_deny_entries_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_get_by_symbol_name(*args, **kwargs):
    return 0

def pl_pmms_strategies_in_long_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_in_short_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_pause_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_resume_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_set_status_for_all(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_long_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_short_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_currentcontracts(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_long_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_short_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_is_paused(*args, **kwargs):
    return 0

def pl_pmms_strategy_pause(*args, **kwargs):
    return 0

def pl_pmms_strategy_resume(*args, **kwargs):
    return 0

def pl_pmms_strategy_set_status(*args, **kwargs):
    return 0

def pl_pmms_strategy_symbol(*args, **kwargs):
    return 0

def pl_pointvalue(*args, **kwargs):
    return 0

def pl_portfolio_currencycode(*args, **kwargs):
    return 0

def pl_portfolio_currententries(*args, **kwargs):
    return 0

def pl_portfolio_getmarginpercontract(*args, **kwargs):
    return 0

def pl_portfolio_investedcapital(*args, **kwargs):
    return 0

def pl_portfolio_maxiddrawdown(*args, **kwargs):
    return 0

def pl_portfolio_maxriskequityperpospercent(*args, **kwargs):
    return 0

def pl_portfolio_strategydrawdown(*args, **kwargs):
    return 0

def pl_portfolioentriespriority(*args, **kwargs):
    return 0

def pl_prevclose(*args, **kwargs):
    return 0

def pl_pricescale(*args, **kwargs):
    return 0

def pl_printer(*args, **kwargs):
    return 0

def pl_processmouseevents(*args, **kwargs):
    return 0

def pl_q_ask(*args, **kwargs):
    return 0

def pl_q_asksize(*args, **kwargs):
    return 0

def pl_q_bid(*args, **kwargs):
    return 0

def pl_q_bidsize(*args, **kwargs):
    return 0

def pl_q_bigpointvalue(*args, **kwargs):
    return 0

def pl_q_date(*args, **kwargs):
    return 0

def pl_q_exchangelisted(*args, **kwargs):
    return 0

def pl_q_last(*args, **kwargs):
    return 0

def pl_q_openinterest(*args, **kwargs):
    return 0

def pl_q_previousclose(*args, **kwargs):
    return 0

def pl_q_time(*args, **kwargs):
    return 0

def pl_q_time_dt(*args, **kwargs):
    return 0

def pl_q_time_s(*args, **kwargs):
    return 0

def pl_q_totalvolume(*args, **kwargs):
    return 0

def pl_raiseruntimeerror(*args, **kwargs):
    return 0

def pl_random(*args, **kwargs):
    return 0

def pl_recalclastbarafter(*args, **kwargs):
    return 0

def pl_recalcpersist(*args, **kwargs):
    return 0

def pl_regularsession(*args, **kwargs):
    return 0

def pl_revsize(*args, **kwargs):
    return 0

def pl_rgb(*args, **kwargs):
    return 0

def pl_rgbtolegacycolor(*args, **kwargs):
    return 0

def pl_sameexitfromoneentryonce(*args, **kwargs):
    return 0

def pl_scrolltobar(*args, **kwargs):
    return 0

def pl_secondsfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    total_seconds = int(round(frac * 86400))
    hours = total_seconds // 3600
    minutes = (total_seconds - hours * 3600) // 60
    seconds = total_seconds - hours * 3600 - minutes * 60
    return seconds

def pl_sess1endtime(*args, **kwargs):
    # Sess1EndTime (backward-compat word, pdf:9083) — regular-session end time.
    # GT instrument session template (NQ 1-min capture): the traded session runs
    # 0830→1515 ET; the last bar of each day closes at 1515. Constant chart config.
    return 1515

def pl_sess1firstbartime(*args, **kwargs):
    # Sess1FirstBarTime (pdf:9087, example "returns 0945") — time of the first bar
    # of session 1. For this chart's 0830-open 1-min session the first bar closes at
    # 0831. Constant chart config.
    return 831

def pl_sess1starttime(*args, **kwargs):
    """Sess1StartTime: start time of the regular trading session (ET).
    For NQ / CME equities, the regular session starts at 08:30 ET (830 in EL HHmm format)."""
    return 830

def pl_sess2endtime(*args, **kwargs):
    # Sess2EndTime (backward-compat, pdf:9101). Single-session template → same end
    # time as session 1 (0830-1515 → 1515). Constant chart config.
    return 1515

def pl_sess2firstbartime(*args, **kwargs):
    # Sess2FirstBarTime (pdf:9105, example "returns 1725"). Single-session template →
    # same first-bar time as session 1 (0831). Constant chart config.
    return 831

def pl_sess2starttime(*args, **kwargs):
    # Sess2StartTime (backward-compat, pdf:9113). Session opens 0830 (first 1-min bar
    # closes at 0831). Constant chart config.
    return 830

def pl_sessioncount(*args, **kwargs):
    # SessionCount(SessionType) (pdf:9120) — number of sessions per week in the
    # template. This NQ chart uses the standard 5-day (Mon-Fri) week → 5 sessions.
    return 5

def pl_sessioncountms(*args, **kwargs):
    # SessionCountMS (pdf:9133) — money-management session count; same 5-day week.
    return 5

def pl_sessionendday(*args, **kwargs):
    # SessionEndDay(SessionType,SessionNum) (pdf:9147) — day-of-week the session ends.
    # The aligned ETH template's session 1 ends on day 1 (Monday).
    return 1

def pl_sessionenddayms(*args, **kwargs):
    # SessionEndDayMS(SessionNum) (pdf:9164) — same end day (1) for the MM session.
    return 1

def pl_sessionendtime(*args, **kwargs):
    """SessionEndTime: end time of the specified trading session and day.
    For NQ on a CME equity-index template (ETH+regular), session 1 (ETH) ends at 16:00 ET (1600)."""
    return 1600

def pl_sessionendtimems(*args, **kwargs):
    # SessionEndTimeMS(SessionNum) (pdf:9194) — MM/regular session end time. The
    # traded session ends 1515 (last bar of day). Constant chart config.
    return 1515

def pl_sessionlastbar(*args, **kwargs):
    # SessionLastBar (pdf:3775) → logical: True on the last bar of the trading
    # session. The runner derives this from the bar date series (next bar is a new
    # day) and passes it via the 'session_last_bar' kwarg, mirroring CurrentSession.
    # Fallback 0 when no runner override (e.g. live last-bar context).
    return kwargs.get('session_last_bar', 0)

def pl_sessionstartday(*args, **kwargs):
    # SessionStartDay(SessionType,SessionNum) (pdf:9206) — day-of-week the session
    # starts. The aligned ETH template's session 1 starts on day 0 (Sunday evening).
    return 0

def pl_sessionstartdayms(*args, **kwargs):
    # SessionStartDayMS(SessionNum) (pdf:9223) — MM session start day = 1 (Monday).
    return 1

def pl_sessionstarttime(*args, **kwargs):
    """SessionStartTime(SessionNumber, DayNumber): start time of the specified session and day.
    Returns time in HHmm format. For NQ on a CME aligned session template:
    session 1 (ETH) starts at 17:00 ET (1700)."""
    return 1700

def pl_currentsession(*args, **kwargs):
    """Return current trading session index.
    Default: 2 (the most common value for NQ aligned-session charts).
    Overridden by 'currentsession' kwarg from the runner."""
    return kwargs.get('currentsession', 2)

def eldatetodatetime(el_date):
    """Convert EL date (YYYYMMDD int) to datetime (stub).
    For chart/commentary use; not affect on trade logic."""
    import datetime
    if isinstance(el_date, (int, float)):
        y = int(el_date) // 10000
        m = (int(el_date) // 100) % 100
        d = int(el_date) % 100
        try:
            return datetime.datetime(y, m, d)
        except ValueError:
            return datetime.datetime(2020, 1, 1)
    return datetime.datetime(2020, 1, 1)

def pl_sessionstarttimems(*args, **kwargs):
    # SessionStartTimeMS(SessionNum) (pdf:9253) — MM/regular session start time.
    # Session opens 0830. Constant chart config.
    return 830

def pl_setbreakeven_pt(*args, **kwargs):
    return 0

def pl_setcustomfitnessvalue(*args, **kwargs):
    return 0

def pl_setfpcompareaccuracy(*args, **kwargs):
    return 0

def pl_setmaxbarsback(*args, **kwargs):
    return 0

def pl_setpercenttrailing_pt(*args, **kwargs):
    return 0

def pl_setprofittarget_pt(*args, **kwargs):
    return 0

def pl_setstopcontract(*args, **kwargs):
    return 0

def pl_setstoploss_pt(*args, **kwargs):
    return 0

def pl_setstopshare(*args, **kwargs):
    return 0

def pl_settrailingstop_pt(*args, **kwargs):
    return 0

def pl_sine(*args, **kwargs):
    import math
    if args:
        return f32(math.sin(math.radians(float(args[0]))))
    return 0.0

def pl_slippage(*args, **kwargs):
    return 0

def pl_squareroot(*args, **kwargs):
    import math
    if args:
        return f32(math.sqrt(float(args[0])))
    return 0.0

def pl_strike(*args, **kwargs):
    return 0

def pl_stringtodate(*args, **kwargs):
    # StringToDate("MM/dd/yyyy") -> DateTime serial integer (pdf:4873/4881
    # StringToDate("01/01/2008")=39448).
    if not args:
        return 0
    p = str(args[0]).strip().split('/')
    mm, dd, yy = int(p[0]), int(p[1]), int(p[2])
    if yy < 100:
        yy = 1900 + yy if yy >= 50 else 2000 + yy
    return float(_ole_from_ymd(yy, mm, dd))

def pl_stringtodatetime(*args, **kwargs):
    # StringToDateTime("MM/dd/yyyy hh:mm:ss tt") -> serial+frac (pdf:4904/4914
    # StringToDateTime("01/01/2008 08:00:00 AM")=39448.33333333).
    if not args:
        return 0
    datepart, _, timepart = str(args[0]).strip().partition(' ')
    return pl_stringtodate(datepart) + _parse_time_frac(timepart)

def pl_stringtodtformatted(*args, **kwargs):
    # StringToDTFormatted("DateTimeString","FormatString") -> serial(+frac)
    # (pdf:4940/4946). e.g. ("02/17/11","MM/dd/yy") -> 40591 (Feb 17 2011).
    if len(args) < 2:
        return 0
    return _parse_dt_formatted(str(args[0]), str(args[1]))

def pl_stringtotime(*args, **kwargs):
    # StringToTime("hh:mm:ss tt") -> fraction of day (pdf:5000/5009).
    if not args:
        return 0.0
    return _parse_time_frac(str(args[0]))

def pl_symbol(*args, **kwargs):
    # Symbol: instrument ticker string (unimplemented). The loud-stub installer
    # wraps this; _UNIMPL_STR_RETURNING makes the non-strict fallback yield ''.
    return 0

def pl_symbol_close(*args, **kwargs):
    return 0

def pl_symbol_date(*args, **kwargs):
    return 0

def pl_symbol_downticks(*args, **kwargs):
    return 0

def pl_symbol_high(*args, **kwargs):
    return 0

def pl_symbol_low(*args, **kwargs):
    return 0

def pl_symbol_open(*args, **kwargs):
    return 0

def pl_symbol_openint(*args, **kwargs):
    return 0

def pl_symbol_tickid(*args, **kwargs):
    return 0

def pl_symbol_ticks(*args, **kwargs):
    return 0

def pl_symbol_time(*args, **kwargs):
    return 0

def pl_symbol_time_s(*args, **kwargs):
    return 0

def pl_symbol_upticks(*args, **kwargs):
    return 0

def pl_symbol_volume(*args, **kwargs):
    return 0

def pl_symbolcurrencycode(*args, **kwargs):
    return 0

def pl_symbolname(*args, **kwargs):
    # SymbolName: instrument name string (unimplemented; loud-stub -> '').
    return 0

def pl_tangent(*args, **kwargs):
    import math
    if args:
        return f32(math.tan(math.radians(float(args[0]))))
    return 0.0

def pl_text_anchor_to_bars(*args, **kwargs):
    return 0

def pl_text_get_anchor_to_bars(*args, **kwargs):
    return 0

def pl_text_getactive(*args, **kwargs):
    return 0

def pl_text_getattribute(*args, **kwargs):
    return 0

def pl_text_getbarnumber(*args, **kwargs):
    return 0

def pl_text_getbgcolor(*args, **kwargs):
    return 0

def pl_text_getborder(*args, **kwargs):
    return 0

def pl_text_getcolor(*args, **kwargs):
    return 0

def pl_text_getdate(*args, **kwargs):
    return 0

def pl_text_getfirst(*args, **kwargs):
    return 0

def pl_text_getfontname(*args, **kwargs):
    return 0

def pl_text_gethstyle(*args, **kwargs):
    return 0

def pl_text_getlock(*args, **kwargs):
    return 0

def pl_text_getnext(*args, **kwargs):
    return 0

def pl_text_getsize(*args, **kwargs):
    return 0

def pl_text_gettime(*args, **kwargs):
    return 0

def pl_text_gettime_dt(*args, **kwargs):
    return 0

def pl_text_gettime_s(*args, **kwargs):
    return 0

def pl_text_getvalue(*args, **kwargs):
    return 0

def pl_text_getvstyle(*args, **kwargs):
    return 0

def pl_text_lock(*args, **kwargs):
    return 0

def pl_text_new_bn(*args, **kwargs):
    return 0

def pl_text_new_dt(*args, **kwargs):
    return 0

def pl_text_setattribute(*args, **kwargs):
    return 0

def pl_text_setbarnumber(*args, **kwargs):
    return 0

def pl_text_setbgcolor(*args, **kwargs):
    return 0

def pl_text_setborder(*args, **kwargs):
    return 0

def pl_text_setlocation_bn(*args, **kwargs):
    return 0

def pl_text_setlocation_dt(*args, **kwargs):
    return 0

def pl_text_setlocation_s(*args, **kwargs):
    return 0

def pl_text_setsize(*args, **kwargs):
    return 0

def pl_tickid(*args, **kwargs):
    return 0

def pl_time2time_s(*args, **kwargs):
    # Time2Time_s(HHmm) -> HHmmss (pdf:5045 1015 -> 101500).
    if not args:
        return 0
    return int(args[0]) * 100

def pl_time_s2time(*args, **kwargs):
    # Time_s2Time(HHmmss) -> HHmm, seconds truncated (pdf:5076 101520 -> 1015).
    if not args:
        return 0
    return int(args[0]) // 100

def pl_eltimetodatetime_s(*args, **kwargs):
    """ELTimeToDateTime_s(HHmmss) -> time-of-day fraction of a DateTime serial
    (pdf:4516 ELTimeToDateTime_s(101525)=0.427372685)."""
    if not args:
        return 0.0
    t = int(args[0])
    hh, mm, ss = t // 10000, (t // 100) % 100, t % 100
    return (hh * 3600 + mm * 60 + ss) / 86400.0

def pl_barstatus(*args, **kwargs):
    """BarStatus(DataN): bar-completeness status. On closed historical bars the
    last tick is always present, so EL returns 2 (closing tick of the bar)."""
    return 2

def pl_tl_anchor_to_bars(*args, **kwargs):
    return 0

def pl_tl_get_anchor_to_bars(*args, **kwargs):
    return 0

def pl_tl_getactive(*args, **kwargs):
    return 0

def pl_tl_getalert(*args, **kwargs):
    return 0

def pl_tl_getbegin_bn(*args, **kwargs):
    return 0

def pl_tl_getbegin_dt(*args, **kwargs):
    return 0

def pl_tl_getbegindate(*args, **kwargs):
    return 0

def pl_tl_getbegintime(*args, **kwargs):
    return 0

def pl_tl_getbegintime_s(*args, **kwargs):
    return 0

def pl_tl_getcolor(*args, **kwargs):
    return 0

def pl_tl_getend_bn(*args, **kwargs):
    return 0

def pl_tl_getend_dt(*args, **kwargs):
    return 0

def pl_tl_getenddate(*args, **kwargs):
    return 0

def pl_tl_getendtime(*args, **kwargs):
    return 0

def pl_tl_getendtime_s(*args, **kwargs):
    return 0

def pl_tl_getextleft(*args, **kwargs):
    return 0

def pl_tl_getextright(*args, **kwargs):
    return 0

def pl_tl_getfirst(*args, **kwargs):
    return 0

def pl_tl_getlock(*args, **kwargs):
    return 0

def pl_tl_getnext(*args, **kwargs):
    return 0

def pl_tl_getsize(*args, **kwargs):
    return 0

def pl_tl_getstyle(*args, **kwargs):
    return 0

def pl_tl_getvalue(*args, **kwargs):
    return 0

def pl_tl_getvalue_bn(*args, **kwargs):
    return 0

def pl_tl_getvalue_dt(*args, **kwargs):
    return 0

def pl_tl_getvalue_s(*args, **kwargs):
    return 0

def pl_tl_lock(*args, **kwargs):
    return 0

def pl_tl_new_bn(*args, **kwargs):
    return 0

def pl_tl_new_dt(*args, **kwargs):
    return 0

def pl_tl_setalert(*args, **kwargs):
    return 0

def pl_tl_setbegin_bn(*args, **kwargs):
    return 0

def pl_tl_setbegin_dt(*args, **kwargs):
    return 0

def pl_tl_setbegin_s(*args, **kwargs):
    return 0

def pl_tl_setend_bn(*args, **kwargs):
    return 0

def pl_tl_setend_dt(*args, **kwargs):
    return 0

def pl_tl_setend_s(*args, **kwargs):
    return 0

def pl_tl_setsize(*args, **kwargs):
    return 0

def pl_tool_dashed2(*args, **kwargs):
    return 0

def pl_tool_dashed3(*args, **kwargs):
    return 0

def pl_varsize(*args, **kwargs):
    return 0

def pl_varstartaddr(*args, **kwargs):
    return 0

def pl_yearfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    date_part = int(dt)  # YYYYMMDD
    return date_part // 10000  # YYYY

def pl_yesterday(*args, **kwargs):
    return 0

def pl_file(path):
    """File handle constructor for Print output. Returns path string."""
    return path

def pl_rateofchange(series, length):
    """Rate of Change alias for pl_roc."""
    return pl_roc(series, length)


# --- Install loud stubs (MUST stay at end of module) ------------------------
def _install_loud_stubs():
    """Wrap every trivial `return 0/None`/`pass` pl_* function so it records the
    call (and raises under PL_STRICT) instead of silently returning 0. Real
    implementations (anything that returns an expression) are left untouched."""
    import ast as _ast, sys as _sys
    mod = _sys.modules[__name__]
    try:
        # Read our own source with an EXPLICIT utf-8 codec, never the locale
        # default. On a legacy codepage (cp932/936/949) or an ascii/LC_ALL=C
        # locale the default codec would raise UnicodeDecodeError on any
        # non-ASCII byte in this file, which used to SILENTLY disable every
        # loud stub (see the except below) -- the worst failure mode there is:
        # production pl_run would then return plausible-but-wrong 0/'' values.
        with open(__file__, 'r', encoding='utf-8') as _f:
            _tree = _ast.parse(_f.read())
    except Exception as _exc:
        # Fail-loud vs. fail-silent. Under PL_STRICT the whole point is that
        # unimplemented builtins RAISE; if we cannot even introspect the source
        # to install those loud stubs we MUST NOT continue as if all were fine,
        # so we re-raise. Without PL_STRICT we keep the historical tolerant
        # behaviour (leave the trivial stubs returning 0/'') but emit exactly
        # one loud stderr warning so the degraded state is never invisible.
        if _PL_STRICT:
            raise RuntimeError(
                "pl_runtime could not introspect its own source (%s) to install "
                "the fail-loud PL_STRICT stubs; refusing to continue, because "
                "unimplemented EasyLanguage builtins would then silently return "
                "plausible-but-wrong 0/'' values instead of raising. "
                "Original error: %r" % (__file__, _exc))
        _sys.stderr.write(
            "WARNING: pl_runtime could not introspect its own source (%r); the "
            "unimplemented-builtin loud stubs are NOT installed, so out-of-scope "
            "EasyLanguage builtins will silently return 0/'' instead of failing "
            "loud. Set PL_STRICT=1 to make this condition fatal.\n" % (_exc,))
        return  # tolerant fallback: leave stubs as-is (no worse than before)

    def _is_stub(fn):
        body = list(fn.body)
        if body and isinstance(body[0], _ast.Expr) and isinstance(getattr(body[0], 'value', None), _ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]  # drop docstring
        if len(body) == 1 and isinstance(body[0], _ast.Return):
            v = body[0].value
            return v is None or (isinstance(v, _ast.Constant) and v.value in (0, 0.0, None, ''))
        if len(body) == 1 and isinstance(body[0], _ast.Pass):
            return True
        return False

    names = {n.name for n in _ast.walk(_tree)
             if isinstance(n, _ast.FunctionDef) and n.name.startswith('pl_') and _is_stub(n)}

    def _make_loud(nm):
        def _loud(*args, **kwargs):
            return _unimplemented(nm)
        _loud.__name__ = nm
        _loud.__doc__ = "Unimplemented EL builtin (loud stub): records call; raises under PL_STRICT."
        _loud._pl_loud_stub = True
        return _loud

    for nm in names:
        if nm in _UNIMPL_WHITELIST:
            continue
        setattr(mod, nm, _make_loud(nm))


_install_loud_stubs()
