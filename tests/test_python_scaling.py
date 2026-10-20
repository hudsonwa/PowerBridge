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
test_python_scaling.py — the pure-Python path costs O(1) per bar (issue #4).

Persist codegen passes a user variable to a series function as
``(_state['x'] + [x])``. On a plain list that copied the whole history every
bar (O(bars^2) per run). The bar loop's ``SeriesState`` makes it an O(1)
``SeriesView``; these tests pin the view's list semantics and the scaling.

The scaling check counts copied elements, so it is deterministic. A
wall-clock check also runs when PB_PERF=1.
"""
import os
import random
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime._rt_history import (  # noqa: E402
    SeriesHistory, SeriesState, SeriesView,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402


def test_series_view_matches_concatenation():
    base = SeriesHistory([0.0, 1.5, 0.0, 2.25, 3.0])
    plain = list(base)
    view = base + [4.5]
    want = plain + [4.5]
    assert isinstance(view, SeriesView)
    base.append(9.0)  # later appends never show through an existing view
    assert len(view) == len(want)
    assert list(view) == want and view == want and not (view != want)
    for i in range(-len(want), len(want)):
        assert view[i] == want[i]
    for bad in (len(want), -len(want) - 1):
        with pytest.raises(IndexError):
            view[bad]
    bounds = (None, -7, -6, -3, -1, 0, 1, 3, 5, 6, 7)
    for start in bounds:
        for stop in bounds:
            for step in (None, 1, 2, -1, -2):
                s = slice(start, stop, step)
                assert view[s] == want[s], s
                assert type(view[s]) is list
    assert list(reversed(view)) == want[::-1]
    assert 4.5 in view and 9.0 not in view
    assert view.count(0.0) == 2 and view.index(2.25) == 3
    assert view + [7.0] == want + [7.0]
    assert base + [1.0, 2.0] == list(base) + [1.0, 2.0]
    assert [7.0] + view == [7.0] + want and type([7.0] + view) is list
    assert view * 2 == want * 2 and 2 * view == 2 * want
    assert view * 0 == [] and type(view * 2) is list
    with pytest.raises(TypeError):
        (1.0,) + view


def test_series_state_wraps_only_seeded_histories():
    st = SeriesState()
    st['x'] = [0.0]
    st['__pb_slots'] = [0, 0]
    st['__pb_seeded'] = 1
    arr = [1, 2, 3]
    st['arr'] = arr
    assert type(st['x']) is SeriesHistory
    assert type(st['__pb_slots']) is list
    assert st['arr'] is arr


def _bars(n, seed=4):
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


def _per_bar_seconds(src, bars, cfg, repeats=3):
    best = float('inf')
    for _ in range(repeats):
        t0 = time.perf_counter()
        run_oracle(src, bars, cfg)
        best = min(best, time.perf_counter() - t0)
    return best / len(bars)


def _ma_cross():
    with open(os.path.join(REPO, 'examples', 'ma_cross.el')) as f:
        return f.read()


def _count_copies(monkeypatch, src, bars):
    """Run ``src`` and return (elements copied by history ``+``, views made,
    history types seen)."""
    stats = {'copied': 0, 'views': 0, 'types': set()}
    orig_add = SeriesHistory.__add__
    orig_tolist = SeriesView.tolist
    orig_set = SeriesState.__setitem__

    def add(self, other):
        out = orig_add(self, other)
        if isinstance(out, SeriesView):
            stats['views'] += 1
        else:
            stats['copied'] += len(out)
        return out

    def tolist(self):
        out = orig_tolist(self)
        stats['copied'] += len(out)
        return out

    def setitem(self, key, value):
        orig_set(self, key, value)
        if '__pb_seeded' not in self and not key.startswith('__pb_'):
            stats['types'].add(type(dict.__getitem__(self, key)))

    monkeypatch.setattr(SeriesHistory, '__add__', add)
    monkeypatch.setattr(SeriesView, 'tolist', tolist)
    monkeypatch.setattr(SeriesState, '__setitem__', setitem)
    run_oracle(src, bars, get_config('NQ'))
    return stats['copied'], stats['views'], stats['types']


def test_history_copies_are_linear_in_bars(monkeypatch):
    src = _ma_cross()
    small = _count_copies(monkeypatch, src, _bars(1000))
    large = _count_copies(monkeypatch, src, _bars(4000))
    # Lookback histories are seeded as SeriesHistory, so `+ [x]` is a view.
    assert small[2] and small[2] <= {SeriesHistory}, small[2]
    assert small[1] >= 1000 and large[1] >= 4 * small[1] - 4
    # Views are made per bar; a full-history copy per bar would be ~bars^2/2
    # elements (2M at 2k bars). Linear growth allows ~4x from 1k to 4k bars.
    assert large[0] <= 4 * small[0] + 4000, (small[0], large[0])
    assert large[0] <= 4 * 4000, large[0]


@pytest.mark.skipif(os.environ.get('PB_PERF') != '1',
                    reason='wall-clock check; set PB_PERF=1 to run')
def test_per_bar_cost_is_flat_2k_to_8k():
    src = _ma_cross()
    cfg = get_config('NQ')
    run_oracle(src, _bars(500), cfg)  # warm imports and transpile caches
    small = _per_bar_seconds(src, _bars(2000), cfg)
    large = _per_bar_seconds(src, _bars(8000), cfg)
    # Linear is ~1.0x; the old O(bars^2) history copy was well above 1.5x.
    # CI runners are noisy, so allow more headroom there.
    limit = 2.0 if os.environ.get('CI') else 1.5
    assert large <= small * limit, (
        f'per-bar cost grew {large / small:.2f}x from 2k to 8k bars '
        f'({small * 1e6:.1f} -> {large * 1e6:.1f} us/bar)')
