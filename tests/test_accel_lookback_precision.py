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

"""Native-vs-Python parity for bar lookbacks (``Close[n]`` and friends).

The Python runtime rounds only the current bar's price to f32 (the hoisted
``_close``). A lookback ``Close[n]`` reads the raw ``close[-(n + 1)]`` list,
even for a literal ``[0]``, and so does its short-history fallback
``open[-1]``. User-variable history holds the stored (already f32) values.
The native kernel used to round every OHLCV lookback to f32, so a program
comparing ``Close - Close[1]`` near a threshold filled differently.

Each program runs on a random-walk tape with sub-tick prices. The trade digest
and the traced variables must match, and the native path must really run.
"""
import os
import random
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config('NQ')


def random_walk(n, seed):
    rng = random.Random(seed)
    price = 15000.0
    bars = []
    for i in range(n):
        o = price + rng.uniform(-3, 3)
        c = o + rng.gauss(0, 6)
        h = max(o, c) + abs(rng.gauss(0, 3))
        lo = min(o, c) - abs(rng.gauss(0, 3))
        price = c
        bars.append({
            'date': 1240101 + i // 400,
            'time': 930 + (i % 400) // 60 * 100 + i % 60,
            'open': round(o, 7), 'high': round(h, 7),
            'low': round(lo, 7), 'close': round(c, 7),
            'volume': float(rng.randint(1, 5000)),
        })
    return bars


def _series_program(series, index):
    # V and W are read, so the native trace carries them.
    return (
        "Inputs: K(2);\nVars: J(0), V(0), W(0);\n"
        "J = Mod(BarNumber, 4);\n"
        f"V = {series}[{index}];\n"
        f"W = {series} - {series}[{index}];\n"
        f"if W > 0 and V > {series}[1] then Buy next bar at market;\n"
        f"if W[1] < -0.25 or V < {series}[J] then SellShort next bar at market;\n"
    )


PROGRAMS = {
    f'{s}[{k}]': _series_program(s, k)
    for s in ('Open', 'High', 'Low', 'Close', 'Volume')
    for k in ('0', '1', '3', 'K', 'J')
}
PROGRAMS.update({
    # Lookback past the start of history falls back to the raw current Open.
    'fallback': (
        "Vars: V(0);\nV = Close[50];\n"
        "if BarNumber < 50 and Close > V then Buy next bar at market;\n"
        "if BarNumber < 50 and Close < V then SellShort next bar at market;\n"
    ),
    'user_var': (
        "Vars: X(0), Y(0);\nX = Close * 1.1 + 0.03;\nY = X[1] - X[2];\n"
        "if X[1] > X[3] + 0.1 and Y > 0 then Buy next bar at market;\n"
        "if X[2] > X[0] then SellShort next bar at market;\n"
    ),
    'accumulator': (
        "Vars: A(0), B(0);\nA = A + Close - Close[1];\nB = A[1] - A[2];\n"
        "if B > 0.5 then Buy next bar at market;\n"
        "if A[3] > A + 0.5 then SellShort next bar at market;\n"
    ),
    'average': (
        "Vars: M(0);\nM = Average(Close, 3);\n"
        "if M[1] > Close[2] and Average(High, 4) > High[1] then Buy next bar at market;\n"
        "if M[2] > M then SellShort next bar at market;\n"
    ),
    'three_conditions': (
        "if Close - Close[1] > 0.3 and Close[1] - Close[2] > Close[2] - Close[3]"
        " and Close[3] < Close[1] then Buy next bar at market;\n"
        "if Close[1] - Close > 0.3 then SellShort next bar at market;\n"
    ),
})


@pytest.fixture(scope='module')
def accel():
    try:
        import pl_transpiler._accel as module
    except ImportError:
        pytest.skip(ACCEL_SKIP)
    return module


@pytest.fixture(scope='module')
def tape():
    return random_walk(1500, seed=11)


def _assert_digest_parity(source, bars):
    native = run_oracle(source, bars, NQ, use_native=True)
    assert native['native'], native.get('native_fallback_reason')
    python = run_oracle(source, bars, NQ)
    assert native['fills'] == python['fills']
    assert native['text'] == python['text']
    return native['fills']


@pytest.mark.parametrize('name', sorted(PROGRAMS))
def test_lookback_digest_and_trace_match_python(accel, tape, name):
    from pl_transpiler.runtime.accel import run_native_trace

    source = PROGRAMS[name]
    _assert_digest_parity(source, tape)

    columns, rows, _ = run_native_trace(source, tape, NQ)
    python = run_el(source, tape, NQ)
    # The Python trace also lists inputs; compare every native column.
    assert set(columns) <= set(python['columns'])
    pick = [python['columns'].index(c) for c in columns]
    assert len(rows) == len(python['rows'])
    for bar, (got, want) in enumerate(zip(rows, python['rows'])):
        assert [str(v) for v in got] == [want[i] for i in pick], (
            f'{name}: trace differs on bar {bar + 1}')

def test_close_minus_close1_threshold_on_sample_bars(accel):
    # f32(Close[1]) made this fire natively; the raw value does not.
    bars = load_bars_csv(os.path.join(REPO, 'examples', 'NQ_sample_bars.csv'))
    source = ("if BarNumber = 3 and Close - Close[1] > 8.179648437499964"
              " then Buy next bar at market;")
    assert _assert_digest_parity(source, bars) == 0


def test_three_condition_lookbacks_long_random_walk(accel):
    bars = random_walk(20000, seed=7)
    assert _assert_digest_parity(PROGRAMS['three_conditions'], bars) > 0
