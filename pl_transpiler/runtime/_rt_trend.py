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

"""PowerLanguage runtime: regression / trend indicators (LinearRegAngle, ADX,
ADXR, DMI, Bollinger bands, Stochastic).

Re-exported by pl_runtime (and by _rt_indicators); see that module."""

from ._rt_core import f32
from ._rt_indicators import _find_first_valid, _ind_cache, _valid_window

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

def _adx_engine(high_s, low_s, close_s, length_val):
    """Incremental Wilder ADX engine. Carries the running smoothed TR/+DM/-DM and
    ADX state across bars (keyed by the series ids + length), extending by the new
    bars only instead of recomputing the whole history every call (O(n) per run,
    not O(n^2)). Reproduces the EXACT float op sequence of a full recompute; reset
    between runs via reset_indicator_caches(). Returns the state dict, whose
    st['vals'][b] is the ADX value as of bar b (== a full recompute over
    series[:b+1])."""
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
    # ADX result is rounded to f32 (see pl_rsi note / GT1 runsum). An
    # f32-per-step model drifts past tolerance for the long-memory recursion at full
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
    Incremental via _adx_engine; byte-identical to a full recompute."""
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
    every call (O(n^2) over a run); reset per run via reset_indicator_caches().
    Gives the same bytes as a full recompute. NOTE: the DM/TR here use RAW double
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
    by the new bar only (rebuilding the whole %K/%SlowK history every call would
    be O(n^2)); the %D/SlowK/SlowD readouts derive from the cached tail. Reset
    per run via reset_indicator_caches(). Gives the same bytes as a full recompute."""
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

        n = len(fk)  # == valid bar count
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
