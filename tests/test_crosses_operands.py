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
test_crosses_operands.py — `crosses` with an operand that is not a series.

A cross compares the operands on this bar and on earlier bars. A function
call (`Average(Close, 10)`), a literal or an offset (`Close[3]`) used to reach
the runtime as a single value, and the cross silently never fired. Each such
operand now gets a hidden per-site history, so the strategy trades exactly as
if the operand were first assigned to a variable.
"""
import os
import random
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.runtime import pl_runtime  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402


def _bars(n, seed=7):
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


BARS = _bars(600)
LEVEL = sorted(b['close'] for b in BARS)[len(BARS) // 2]
OPERANDS = ['Average(Close, 10)', f'{LEVEL:.2f}', 'Close[3]']


def _inline(x, swap):
    a, b = (x, 'Close') if swap else ('Close', x)
    up, dn = ('below', 'above') if swap else ('above', 'below')
    return (f"If {a} crosses {up} {b} then Buy next bar at market;\n"
            f"If {a} crosses {dn} {b} then SellShort next bar at market;\n")


def _variable(x, swap):
    return f"Vars: X(0);\nX = {x};\n" + _inline('X', swap)


@pytest.mark.parametrize('swap', [False, True], ids=['left', 'right'])
@pytest.mark.parametrize('operand', OPERANDS)
def test_non_series_operand_trades_like_variable(operand, swap):
    cfg = get_config('NQ')
    inline = run_oracle(_inline(operand, swap), BARS, cfg)
    variable = run_oracle(_variable(operand, swap), BARS, cfg)
    assert variable['round_turns'] > 5  # the case actually crosses
    assert inline['sha256'] == variable['sha256']
    assert inline['round_turns'] == variable['round_turns']


def test_literal_on_both_sides():
    src = "If Close[1] crosses above 15000 then Buy next bar at market;\n"
    assert 'pl_crosses_above(' in transpile(src, persist=True)
    var = "Vars: A(0), B(0);\nA = Close[1]; B = 15000;\n" \
          "If A crosses above B then Buy next bar at market;\n"
    cfg = get_config('NQ')
    assert (run_oracle(src, BARS, cfg)['sha256']
            == run_oracle(var, BARS, cfg)['sha256'])


def test_hoisted_operand_is_evaluated_every_bar():
    # A short-circuited condition must not leave gaps in the history.
    gated = ("If MarketPosition = 0 and Close crosses above Average(Close, 10) "
             "then Buy next bar at market;\n"
             "If MarketPosition = 1 and Close crosses below Average(Close, 10) "
             "then Sell next bar at market;\n")
    var = ("Vars: A(0);\nA = Average(Close, 10);\n"
           "If MarketPosition = 0 and Close crosses above A then Buy next bar at market;\n"
           "If MarketPosition = 1 and Close crosses below A then Sell next bar at market;\n")
    cfg = get_config('NQ')
    assert (run_oracle(gated, BARS, cfg)['sha256']
            == run_oracle(var, BARS, cfg)['sha256'])


@pytest.mark.parametrize('operand', ['Value1 + 1', 'Average(Value1, 5)', 'MarketPosition'])
def test_operand_that_changes_within_the_bar_is_refused(operand):
    src = f"Value1 = Close;\nIf Close crosses above {operand} then Buy next bar at market;\n"
    with pytest.raises(ValueError, match='unsupported crosses operand'):
        transpile(src, persist=True)


def test_refusal_names_the_line():
    src = "Vars: L(10);\nIf Close crosses above Average(Close, L) then Buy next bar at market;\n"
    with pytest.raises(ValueError, match='^line 2: '):
        transpile(src, persist=True)


def test_value_scratch_vars_cross_like_declared_vars():
    # Undeclared Value1..Value99 keep their value across bars, so crossing
    # them works exactly like crossing declared variables.
    body = ("{a} = Average(Close, 9);\n{b} = Average(Close, 21);\n"
            "If {a} crosses above {b} then Buy next bar at market;\n"
            "If {a} crosses below {b} then Sell next bar at market;\n")
    scratch = body.format(a='Value1', b='Value2')
    declared = "Vars: A(0), B(0);\n" + body.format(a='A', b='B')
    cfg = get_config('NQ')
    want = run_oracle(declared, BARS, cfg)
    assert want['round_turns'] > 5
    assert run_oracle(scratch, BARS, cfg)['sha256'] == want['sha256']


def test_runtime_refuses_a_scalar():
    with pytest.raises(TypeError, match='not a series'):
        pl_runtime.pl_crosses_above([1.0, 2.0], 1.5)
    with pytest.raises(TypeError, match='not a series'):
        pl_runtime.pl_crosses_below(1.5, [1.0, 2.0])
