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

from typing import Any as _Any

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
    # `length` elements are all valid and series[-length:] equals the
    # valid-only tail.
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

def _cross_prior_relation(series_a, series_b, start, off_a=0, off_b=0):
    """Return the sign of (A - B) on the bar that establishes the pre-cross
    relation, walking back through any consecutive run of equal bars
    (pdf:2502-2509 / 2580-2587). EL defines a cross relative to the last bar
    BEFORE the current one on which A and B differed: a run of equal bars
    immediately preceding the current bar is transparent, and the relation is
    taken from the bar before that run. Returns -1 if A<B, +1 if A>B, 0 if no
    differing prior bar exists (all-equal history -> no cross).

    ``i`` counts bars in the common, right-aligned window of the two
    histories; ``off_a`` / ``off_b`` are how many extra leading items each
    side holds (see ``_cross_operands``)."""
    i = len(series_a) - off_a - 2  # bar immediately preceding the current bar
    while i >= start:
        a, b = series_a[i + off_a], series_b[i + off_b]
        if a < b:
            return -1
        if a > b:
            return 1
        i -= 1  # equal on this bar: keep walking back through the equal run
    return 0

def _require_cross_series(series_a, series_b):
    """A cross compares this bar with earlier bars, so both operands must be
    histories. Codegen gives every non-series operand its own history; a
    scalar here would otherwise never cross, so refuse it."""
    for s in (series_a, series_b):
        if not hasattr(s, '__len__'):
            raise TypeError(
                f"crosses operand {s!r} is a single value, not a series; "
                f"a cross needs the operand's earlier values")

def _cross_operands(series_a, series_b):
    """Normalise crosses operands. Returns (off_a, off_b, start), or start
    None when no cross is possible yet. Both sides must be
    series (see ``_require_cross_series``). Leading zeros are the
    runner's PAD element and indicator warm-up, so they are skipped, but only
    on a side that has a nonzero value: an all-zero side is a zero level (for
    example a declared ``Z(0)``), and skipping it would never start the cross.

    The two histories end on the current bar but may differ in length: under
    MaxBarsBack a price series holds the skipped bars too, while a variable's
    history starts on the first evaluated bar. They are compared right-aligned
    over the bars both hold; ``off_a`` / ``off_b`` are each side's extra
    leading items and ``start`` indexes that common window."""
    _require_cross_series(series_a, series_b)
    if len(series_a) < 2 or len(series_b) < 2:
        return 0, 0, None
    n = min(len(series_a), len(series_b))
    off_a = len(series_a) - n
    off_b = len(series_b) - n
    start_a = max(_find_first_valid(series_a) - off_a, 0)
    start_b = max(_find_first_valid(series_b) - off_b, 0)
    if start_a == n:
        start = start_b
    elif start_b == n:
        start = start_a
    else:
        start = max(start_a, start_b)
    if n - start < 2:
        return off_a, off_b, None
    return off_a, off_b, start

def pl_crosses_above(series_a, series_b):
    """True if series_a crossed above series_b on this bar.
    Skips leading padding / warm-up zeros so the first real bar never fires a
    false cross; a side that is constant (a literal, or all zeros so far) is a
    level, not a warm-up, and is compared from the other side's first bar.
    A run of equal bars immediately preceding the current bar is transparent;
    the pre-cross relation is taken from the bar before that run (pdf:2502-2509)."""
    off_a, off_b, start = _cross_operands(series_a, series_b)
    if start is None:
        return False
    return (series_a[-1] > series_b[-1]
            and _cross_prior_relation(series_a, series_b, start, off_a, off_b) < 0)

def pl_crosses_below(series_a, series_b):
    """True if series_a crossed below series_b on this bar.
    Skips leading padding / warm-up zeros so the first real bar never fires a
    false cross; a side that is constant (a literal, or all zeros so far) is a
    level, not a warm-up, and is compared from the other side's first bar.
    A run of equal bars immediately preceding the current bar is transparent;
    the pre-cross relation is taken from the bar before that run (pdf:2561-2569)."""
    off_a, off_b, start = _cross_operands(series_a, series_b)
    if start is None:
        return False
    return (series_a[-1] < series_b[-1]
            and _cross_prior_relation(series_a, series_b, start, off_a, off_b) > 0)

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
_ind_cache: dict[_Any, _Any] = {}


def reset_indicator_caches():
    """Clear the incremental indicator state. MUST be called at the start of every
    run_gt run so a recycled list id() can never serve a stale prior-run cache."""
    _ind_cache.clear()


def _pb_lag(site, series, k):
    """``series[:-k]`` for ``Func(series)[k]``, as one list per call site.

    The bar loop's price histories only grow, so the lagged prefix is kept per
    ``site`` and extended in place instead of re-sliced (an O(bars) copy every
    bar). Same contents and type as the slice; the stable identity also lets
    the incremental indicators (XAverage, RSI, ...) extend instead of
    recomputing, and means the ``id()``-keyed caches can no longer mix up two
    calls: a freed temporary slice used to hand its id (and its state) to the
    next one. Rebuilt whenever ``series`` is a different object."""
    if type(k) is not int:
        return series[:-k]
    n = len(series)
    m = max(slice(None, -k).indices(n)[1], 0)
    key = ('lag', site)
    st = _ind_cache.get(key)
    if st is None or st[0] is not series:
        lst = series[:m]
        _ind_cache[key] = (series, lst)
        return lst
    lst = st[1]
    cur = len(lst)
    if m > cur:
        lst.extend(series[cur:m])
    elif m < cur:
        del lst[m:]
    return lst


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
    runsum/double-cumsum capture). An f32-per-step model stays within
    tolerance at ~1500 bars but drifts past it for the long-memory Wilder recursion
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
    length); byte-identical to a series[start+1:] full recompute."""
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
        if tgt < 0:
            # A negative TgtBar projects the line into the future; its exact
            # MultiCharts result is unverified, so stop instead of guessing.
            from pl_transpiler.errors import UnimplementedKeywordError
            raise UnimplementedKeywordError(
                f"LinearRegValue with a negative TgtBar ({tgt_bar}) is not "
                f"supported yet")
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


def pl_variance(series, length, data_type=None):
    """Variance of last `length` values.

    EL's ``Variance(Price, Length, DataType)``: DataType 1 = population
    (divide by N), 2 = sample (divide by N - 1), the same convention and
    arithmetic as ``pl_std_dev`` (whose result is this one's square root).
    Without DataType, the original two-argument form."""
    if data_type is not None:
        window, n = _valid_window(series, length)
        if n < length or n == 0:
            return 0.0
        mean = f32(sum(window) / length)
        variance = f32(sum(f32((x - mean) ** 2) for x in window) / length)
        if data_type == 2 and length > 1:
            return f32(variance * length / (length - 1))
        return variance
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


def _find_first_valid(series):
    """Find index of first non-zero element in series."""
    for i, v in enumerate(series):
        if v != 0:
            return i
    return len(series)


from ._rt_trend import _adx_engine as _adx_engine  # noqa: E402
from ._rt_trend import _calc_dmi as _calc_dmi  # noqa: E402
from ._rt_trend import _compute_adx_series as _compute_adx_series  # noqa: E402
from ._rt_trend import _tr_rma as _tr_rma  # noqa: E402
from ._rt_trend import _true_range_for_adx as _true_range_for_adx  # noqa: E402
from ._rt_trend import (  # noqa: E402,F401  (re-export)
    pl_adx,
    pl_adxr,
    pl_bollinger_band,
    pl_dmiminus,
    pl_dmiplus,
    pl_linear_reg_angle,
    pl_stochastic,
)
