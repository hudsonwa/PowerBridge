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

"""PowerLanguage runtime: Technical indicators, statistics and regression/trend functions.

Re-exported by pl_runtime; see that module."""

from ._rt_core import f32

def _valid_window(series, length):
    """Return the last `length` values from series, skipping leading padding zeros.
    Returns (window, valid_count) where valid_count is the number of non-padding
    elements. If fewer than `length` valid elements exist, returns (window, <length).

    ``series`` may be a list, ``array.array``, or ``memoryview``. Results are
    bit-identical across those types; the helpers do not convert
    to list. Empty / too-short series still return 0.0 from the callers.
    """
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
    Skips leading padding zeros. Accepts list / array.array / memoryview."""
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

# ---- Incremental indicator cache (vectorization) ----
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
    """Partial-transpile stub factory (opt-in --partial mode).

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
        smoothtype = int(args[6])  # noqa: F841  1=SMA, 2=EMA, etc. (currently only SMA implemented)

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
