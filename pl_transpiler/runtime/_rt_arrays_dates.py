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

"""PowerLanguage runtime: EL constants, array, date/time, logical and list helpers, price/volume
reserved words and daily session data.

Re-exported by pl_runtime; see that module."""

from ._rt_core import f32

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
