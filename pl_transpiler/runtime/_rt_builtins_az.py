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

"""PowerLanguage runtime: Runtime implementations and stubs for the A-Z PowerLanguage builtins.

Re-exported by pl_runtime; see that module."""

from ._rt_core import f32

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
    """EL Array_Compare(SrcArr, SrcIdx, DstArr, DstIdx, Count): compare Count
    elements pairwise from the two start indexes. Returns 0 when the ranges
    are equal, 1 when the first differing source element is greater and -1
    when it is smaller. Out-of-range elements are not compared."""
    if len(args) < 5 or not isinstance(args[0], list) or not isinstance(args[2], list):
        return 0
    si, di, n = int(args[1]), int(args[3]), int(args[4])
    if si < 0 or di < 0 or n <= 0:
        return 0
    src, dst = args[0][::-1], args[2][::-1]
    for a, b in zip(src[si:si + n], dst[di:di + n]):
        if a != b:
            return 1 if a > b else -1
    return 0

def pl_array_contains(*args, **kwargs):
    """EL Array_Contains(arr, value): True/False whether value is in the array
    (pdf:6034-6036)."""
    if len(args) >= 2 and isinstance(args[0], list):
        return args[1] in args[0]
    return False

def pl_array_copy(*args, **kwargs):
    """EL Array_Copy(SrcArr, SrcIdx, DstArr, DstIdx, Count): copy Count
    elements from SrcArr[SrcIdx..] into DstArr[DstIdx..], clipped to both
    arrays' bounds. Overlapping ranges in one array copy as if via a buffer."""
    if len(args) < 5 or not isinstance(args[0], list) or not isinstance(args[2], list):
        return 0
    si, di, n = int(args[1]), int(args[3]), int(args[4])
    if si < 0 or di < 0 or n <= 0:
        return 0
    dst = args[2][::-1]
    chunk = args[0][::-1][si:si + n][:max(len(dst) - di, 0)]
    dst[di:di + len(chunk)] = chunk
    args[2][:] = dst[::-1]
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

def _pl_array_set_range(args, fn):
    """Apply fn to the EL range Arr[BeginIdx..EndIdx] (inclusive, clipped).

    Generated code stores EL index i at list position -(i + 1), so work on the
    reversed list and write it back."""
    arr = args[0][::-1]
    lo, hi = max(int(args[1]), 0), min(int(args[2]), len(arr) - 1)
    if lo <= hi:
        arr[lo:hi + 1] = fn(arr[lo:hi + 1])
        args[0][:] = arr[::-1]

def pl_array_setvalrange(*args, **kwargs):
    """EL Array_SetValRange(Arr, BeginIdx, EndIdx, Value): set Arr[BeginIdx..
    EndIdx] (inclusive, clipped to the array) to Value."""
    if len(args) >= 4 and isinstance(args[0], list):
        _pl_array_set_range(args, lambda seg: [args[3]] * len(seg))
    return 0

def pl_array_sort(*args, **kwargs):
    """EL Array_Sort(Arr, BeginIdx, EndIdx, SortOrder): sort Arr[BeginIdx..
    EndIdx] in place, ascending when SortOrder is True, descending otherwise."""
    if len(args) >= 4 and isinstance(args[0], list):
        _pl_array_set_range(args, lambda seg: sorted(seg, reverse=not args[3]))
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
