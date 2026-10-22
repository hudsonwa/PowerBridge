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
"""Closed-trade performance words (NumWinTrades, PercentProfit, ...) reach the
strategy every bar. They used to read 0 because the per-bar inputs never
carried the engine's running counts."""
from pathlib import Path

import pytest

from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_run import load_bars_csv, run_el

ROOT = Path(__file__).resolve().parent.parent
WORDS = (
    'NumWinTrades', 'NumLosTrades', 'NumEvenTrades', 'TotalTrades',
    'GrossProfit', 'GrossLoss', 'PercentProfit', 'LargestWinTrade',
    'LargestLosTrade', 'MaxConsecWinners', 'MaxConsecLosers',
    'TotalBarsWinTrades', 'TotalBarsLosTrades', 'TotalBarsEvenTrades',
    'AvgBarsWinTrade', 'AvgBarsLosTrade', 'AvgBarsEvenTrade',
)


def _run(reverse):
    src = (ROOT / 'examples' / 'ma_cross.el').read_text()
    if reverse:
        # Contrarian stop-and-reverse, so the trending sample produces
        # losing trades too.
        src = src.replace('Sell ("LX")', 'SellShort ("SE")')
        src = src.replace('crosses above', '@@').replace(
            'crosses below', 'crosses above').replace('@@', 'crosses below')
    names = ['v%d' % i for i in range(len(WORDS))]
    src = src.replace('Vars: Fast(0), Slow(0);',
                      'Vars: Fast(0), Slow(0), %s;'
                      % ', '.join(n + '(0)' for n in names))
    src += ''.join('%s = %s;\n' % (n, w) for n, w in zip(names, WORDS))
    out = run_el(src, load_bars_csv(ROOT / 'examples' / 'NQ_sample_bars.csv'),
                 get_config('NQ'), trace_columns=names)
    rows = [dict(zip(WORDS, map(float, r))) for r in out['rows']]
    return rows, out['trades']


@pytest.mark.parametrize('reverse', [False, True])
def test_counts_reach_strategy(reverse):
    rows, trades = _run(reverse)
    for r in rows:
        w, lo, ev, tt = (r['NumWinTrades'], r['NumLosTrades'],
                         r['NumEvenTrades'], r['TotalTrades'])
        assert w + lo + ev == tt
        if r['GrossProfit'] > 0:
            assert w > 0
        if r['GrossLoss'] < 0:
            assert lo > 0
        assert r['PercentProfit'] == pytest.approx(100.0 * w / tt if tt else 0.0)
        assert r['LargestWinTrade'] <= r['GrossProfit'] + 1e-6
        assert r['LargestLosTrade'] >= r['GrossLoss'] - 1e-6
        assert r['MaxConsecWinners'] <= w and r['MaxConsecLosers'] <= lo
        if w:
            assert r['AvgBarsWinTrade'] == pytest.approx(
                r['TotalBarsWinTrades'] / w)
    last = rows[-1]
    assert last['NumLosTrades' if reverse else 'NumWinTrades'] > 0

    # Rebuild win/loss counts from the engine's own fill list.
    pos, entry, wins, losses = 0, None, 0, 0
    for t in trades:
        side = 1 if t['type'] == 'EntryLong' else -1
        if pos:
            pnl = (t['price'] - entry) * pos
            wins += pnl > 0
            losses += pnl < 0
            pos, entry = 0, None
            if reverse:
                pos, entry = side, t['price']
        else:
            pos, entry = side, t['price']
        if not reverse and side == -1:
            pos, entry = 0, None
    assert (last['NumWinTrades'], last['NumLosTrades']) == (wins, losses)
