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

"""test_array_builtins.py — #36: Array_Compare, Array_Copy, Array_SetValRange
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

from pl_transpiler.runtime import pl_runtime as rt                   # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config       # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle                 # noqa: E402

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
