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

"""test_array_builtins.py — Array_Compare, Array_Copy, Array_SetValRange
and Array_Sort.

These MultiCharts helpers act on index ranges of 1-D arrays. Each is checked
directly on lists laid out like generated code and once end to end inside a strategy.

Stdlib + pytest only, synthetic bars, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime import pl_runtime as rt  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402

NQ = get_config("NQ")
BARS = [{"date": 1240102, "time": 930 + i, "open": 100.0 + i, "high": 101.0 + i,
         "low": 99.0 + i, "close": 100.5 + i, "volume": 1000.0} for i in range(20)]


def _el(*vals):
    """A list laid out like generated code: EL index i at position -(i + 1)."""
    return list(vals)[::-1]


def _trades(el):
    return run_oracle(el, BARS, NQ)["records"]


def test_builtins_are_not_loud_stubs():
    for name in ("pl_array_compare", "pl_array_copy",
                 "pl_array_setvalrange", "pl_array_sort"):
        assert not getattr(getattr(rt, name), "_pl_loud_stub", False), name


def test_array_compare():
    a, b = _el(1, 2, 3, 4), _el(9, 2, 3, 4, 9)
    assert rt.pl_array_compare(a, 1, b, 1, 3) == 0
    assert rt.pl_array_compare(a, 0, b, 0, 1) == -1
    assert rt.pl_array_compare(b, 0, a, 0, 1) == 1
    # The first differing element decides, not later ones.
    assert rt.pl_array_compare(_el(1, 5), 0, _el(2, 0), 0, 2) == -1
    assert rt.pl_array_compare(a, 0, b, 0, 0) == 0


def test_array_copy():
    src, dst = _el(1, 2, 3, 4), _el(*[0] * 6)
    rt.pl_array_copy(src, 1, dst, 2, 3)
    assert dst == _el(0, 0, 2, 3, 4, 0)
    # Clipped at the destination's end, never grows it.
    rt.pl_array_copy(src, 0, dst, 4, 4)
    assert dst == _el(0, 0, 2, 3, 1, 2)
    # Overlap within one array copies the original values.
    arr = _el(1, 2, 3, 4, 5)
    rt.pl_array_copy(arr, 0, arr, 1, 4)
    assert arr == _el(1, 1, 2, 3, 4)


def test_array_setvalrange():
    arr = _el(*[0] * 5)
    rt.pl_array_setvalrange(arr, 1, 3, 7.5)
    assert arr == _el(0, 7.5, 7.5, 7.5, 0)
    rt.pl_array_setvalrange(arr, 3, 99, 1)
    assert arr == _el(0, 7.5, 7.5, 1, 1)


def test_array_sort():
    arr = _el(5, 3, 9, 1, 4)
    rt.pl_array_sort(arr, 1, 3, True)
    assert arr == _el(5, 1, 3, 9, 4)
    rt.pl_array_sort(arr, 0, 4, False)
    assert arr == _el(9, 5, 4, 3, 1)


def test_array_builtins_in_strategy():
    el = """
Vars: Cmp(0) ;
Arrays: FArr[4](0), GArr[8](0) ;
Array_SetValRange( GArr, 0, 3, 5 ) ;
GArr[1] = 2 ;
Array_Sort( GArr, 0, 3, True ) ;
Array_Copy( GArr, 0, FArr, 0, 4 ) ;
Cmp = Array_Compare( FArr, 0, GArr, 0, 4 ) ;
If Cmp = 0 and FArr[0] = 2 and FArr[3] = 5 and MarketPosition = 0 Then
    Buy next bar at market ;
"""
    assert _trades(el), "array builtins did not produce the expected values"


# ---- #40: every array builtin maps EL indexes like direct A[i] access ----

def test_get_set_value_use_el_indexes():
    arr = _el(10, 20, 30, 40)
    assert rt.pl_array_getintegervalue(arr, 0) == 10
    assert rt.pl_array_getfloatvalue(arr, 3) == 40
    assert rt.pl_array_getintegervalue(arr, 4) == 0
    assert rt.pl_array_getintegervalue(arr, -1) == 0
    rt.pl_array_setintegervalue(arr, 1, 99)
    assert arr == _el(10, 99, 30, 40)
    assert rt.pl_array_setintegervalue(arr, 4, 1) == 0
    assert arr == _el(10, 99, 30, 40)


def test_sum_highest_lowest_indexof_use_el_indexes():
    arr = _el(1, 5, -2, 8)
    assert rt.pl_array_sum(arr, 0, 1) == 6
    assert rt.pl_array_highest(arr, 0, 2) == 5
    assert rt.pl_array_lowest(arr, 1, 3) == -2
    assert rt.pl_array_indexof(arr, 8) == 3
    assert rt.pl_array_indexof(arr, 7) == -1


def test_setmaxindex_keeps_existing_elements():
    arr = _el(1, 2)
    assert rt.pl_array_setmaxindex(arr, 3)
    assert arr == _el(1, 2, 0, 0)


def test_sortarray_variancearray_use_el_indexes():
    arr = _el(0, 3, 1, 2, 9)
    rt.pl_sortarray(arr, 3, True)
    assert arr == _el(0, 1, 2, 3, 9)
    assert rt.pl_variancearray(_el(0, 1, 3, 100), 2) == 1.0


def _strategy(cond, setup=""):
    return f"""
Vars: G(0) ;
Arrays: A[3](0) ;
A[0] = 10 ; A[1] = 20 ; A[2] = 30 ; A[3] = 40 ;
{setup}
If {cond} and MarketPosition = 0 Then Buy next bar at market ;
"""


def test_array_helpers_match_direct_indexing_in_strategy():
    cases = [
        ("Array_GetIntegerValue( A, 0 ) = A[0]", ""),
        ("Array_GetFloatValue( A, 3 ) = A[3]", ""),
        ("A[2] = 7 and A[1] = 20", "Array_SetIntegerValue( A, 2, 7 ) ;"),
        ("Array_Sum( A, 0, 1 ) = A[0] + A[1]", ""),
        ("Array_Highest( A, 0, 2 ) = A[2]", ""),
        ("Array_Lowest( A, 1, 3 ) = A[1]", ""),
        ("Array_IndexOf( A, A[1] ) = 1", ""),
    ]
    for cond, setup in cases:
        # The control (a false condition) proves the check can fail.
        assert _trades(_strategy(cond, setup)), cond
        assert not _trades(_strategy(cond + " and A[0] = 0", setup)), cond
