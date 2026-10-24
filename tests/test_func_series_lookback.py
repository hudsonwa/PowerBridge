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

"""
test_func_series_lookback.py — ``Func(series)[n]`` costs O(1) per bar.

Codegen used to pass ``high[:-n]`` for ``Highest(High, Len)[n]``: a copy of
the whole history every bar, so a breakout strategy was O(bars^2) on the
Python path. It now emits ``_pb_lag(site, high, n)``, a per-site prefix
extended in place. These tests check it against the old slice bar by bar and
pin the scaling. The old slices were wrong when two lagged recursive calls on
different series shared an id()-keyed cache entry, so that case is checked
against a from-scratch recompute instead (not in ``CASES``).
"""
import os
import random
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import run as pl_run  # noqa: E402
from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.runtime._rt_indicators import _pb_lag, reset_indicator_caches  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402

BREAKOUT_EL = """
Inputs: Len(20);
If Close > Highest(High, Len)[1] then Buy next bar at market;
If Close < Lowest(Low, Len)[1] then SellShort next bar at market;
"""

# Each value is a lagged function call; plotted so run_el traces every bar.
# Each case has at most one lagged recursive call per length, where the old
# slices were right; see SHARED_CACHE_EL for the case they got wrong.
CASES = {
    'highest_lowest': """
Inputs: Len(10);
Vars: a(0), b(0), c(0);
a = Highest(High, Len)[1];
b = Lowest(Low, Len)[3];
c = Highest(High, Len)[0];
if Close > a then Buy next bar at market;
if Close < b then SellShort next bar at market;
""",
    'average_xaverage': """
Inputs: Len(8);
Vars: a(0), b(0), c(0);
a = Average(Close, Len)[1];
b = XAverage(Close, Len)[2];
c = XAverage(High, 3)[1] - XAverage(High, 3);
if a crosses above b then Buy next bar at market;
if a crosses below b then Sell next bar at market;
""",
    'rsi_dynamic_offset': """
Inputs: Len(14);
Vars: a(0), b(0);
a = RSI(Close, Len)[1];
b = RSI(Close, Len)[CurrentBar - CurrentBar + 2];
if a > 70 then SellShort next bar at market;
if a < 30 then Buy next bar at market;
""",
    'nested': """
Inputs: Len(5);
Vars: a(0), b(0), c(0);
a = Highest(High, Len)[1] - Lowest(Low, Len)[1];
b = Average(Close, Len)[1] + XAverage(Close, Highest(High, 3)[2] - Lowest(Low, 3)[2] + 2)[1];
c = Highest(High, Len)[CurrentBar];
if a > b * 0.001 then Buy next bar at market;
""",
}


def _bars(n, seed=11):
    rng = random.Random(seed)
    bars, p, day, m = [], 15000.0, 1240102, 0
    for _ in range(n):
        if m == 390:
            m, day = 0, day + 1
        t = (570 + m) // 60 * 100 + (570 + m) % 60
        c = round((p + rng.gauss(0, 8)) * 4) / 4
        bars.append({'date': day, 'time': t, 'open': p,
                     'high': max(p, c) + rng.randint(0, 8) * 0.25,
                     'low': min(p, c) - rng.randint(0, 8) * 0.25,
                     'close': c, 'volume': rng.randint(50, 500)})
        p, m = c, m + 1
    return bars


def _old_slices(code):
    """Rewrite each ``_pb_lag(site, s, k)`` back to the old ``s[:-k]``."""
    out, i, tag = [], 0, '_pb_lag('
    while True:
        j = code.find(tag, i)
        if j < 0:
            out.append(code[i:])
            return ''.join(out)
        out.append(code[i:j])
        depth, k = 1, j + len(tag)
        while depth:
            depth += {'(': 1, ')': -1}.get(code[k], 0)
            k += 1
        _site, s, off = (a.strip() for a in
                         _old_slices(code[j + len(tag):k - 1]).split(',', 2))
        out.append(f'{s}[:-{off}]')
        i = k


def test_lag_matches_slice():
    reset_indicator_caches()
    s = []
    for v in range(40):
        s.append(float(v % 7))
        for k in (0, 1, 2, 5, 45, -3, (v * 7) % 6):
            assert _pb_lag(('t', k), s, k) == s[:-k]
            assert _pb_lag('dyn', s, k) == s[:-k]
    other = [1.0, 2.0, 3.0]
    assert _pb_lag('dyn', other, 1) == [1.0, 2.0]
    with pytest.raises(TypeError):
        _pb_lag('f', s, 1.0)


def test_codegen_emits_lag_not_slice():
    code = transpile(CASES['nested'], persist=True)
    assert '_pb_lag(' in code and '[:-' not in code
    assert '[:-' in _old_slices(code) and '_pb_lag(' not in _old_slices(code)


@pytest.mark.parametrize('name', sorted(CASES))
def test_per_bar_equal_to_old_slicing(monkeypatch, name):
    src, bars, cfg = CASES[name], _bars(1500), get_config('NQ')
    new = pl_run.run_el(src, bars, cfg, max_bars_back=0)
    orig = pl_run.transpile

    def old_transpile(*a, **kw):
        return _old_slices(orig(*a, **kw))

    monkeypatch.setattr(pl_run, 'transpile', old_transpile)
    old = pl_run.run_el(src, bars, cfg, max_bars_back=0)
    assert new['columns'] == old['columns']
    assert len(new['rows']) == len(bars)
    assert new['rows'] == old['rows']
    assert new['trades'] == old['trades']


def _seconds(src, bars, cfg):
    best = float('inf')
    for _ in range(2):
        t0 = time.perf_counter()
        run_oracle(src, bars, cfg, max_bars_back=0)
        best = min(best, time.perf_counter() - t0)
    return best


def test_breakout_scales_linearly():
    cfg = get_config('NQ')
    run_oracle(BREAKOUT_EL, _bars(500), cfg, max_bars_back=0)  # warm imports
    small = _seconds(BREAKOUT_EL, _bars(5000), cfg)
    large = _seconds(BREAKOUT_EL, _bars(20000), cfg)
    # 4x the bars: linear is ~4x; the old per-bar slice was ~10x or more here.
    assert large < 8 * small, (small, large)


# Two lagged recursive calls on different series. On the old slices each call
# got a fresh temporary list, so ``high[:-1]`` could reuse the id() of the
# freed ``low[:-1]`` and pick up its id()-keyed EMA/RSI state: wrong values.
SHARED_CACHE_EL = """
Vars: e(0), r(0);
e = XAverage(Low, 10)[1] + XAverage(High, 10)[1];
r = RSI(Low, 14)[1] + RSI(High, 14)[1];
"""


def test_lagged_recursive_pair_matches_recompute():
    from pl_transpiler.runtime.pl_runtime import f32, pl_rsi, pl_xaverage

    bars = _bars(300)
    out = pl_run.run_el(SHARED_CACHE_EL, bars, get_config('NQ'), max_bars_back=0)
    cols = out['columns']
    ie, ir = cols.index('e'), cols.index('r')
    hi = [b['high'] for b in bars]
    lo = [b['low'] for b in bars]
    for i, row in enumerate(out['rows']):
        if i == 0:
            continue
        e = r = 0.0
        for f, n, s in ((pl_xaverage, 10, lo), (pl_xaverage, 10, hi),
                        (pl_rsi, 14, lo), (pl_rsi, 14, hi)):
            reset_indicator_caches()  # from scratch: no state between calls
            v = f(s[:i], n)
            if f is pl_xaverage:
                e += v
            else:
                r += v
        e, r = f32(e), f32(r)
        assert float(row[ie]) == e, (i, row[ie], e)
        assert float(row[ir]) == r, (i, row[ir], r)
    assert float(out['rows'][1][ie]) == bars[0]['high'] + bars[0]['low']
