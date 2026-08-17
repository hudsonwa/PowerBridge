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

import sys, os
sys.path.insert(0, os.path.expanduser('~/Desktop/pl-transpiler'))
from pl_transpiler import transpile


STRATEGY = """\
Variables: Value1(0), Condition1(False);
Value1 = Average(Close, 14);
Condition1 = Close > Value1;
If Condition1 Then Buy Next Bar At Market;
If MarketPosition = 1 And Close < Value1 Then Sell Next Bar At Market;
Plot1(Value1);
"""


def test_complex_syntax():
    result = transpile(STRATEGY)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_complex_content():
    result = transpile(STRATEGY)
    assert 'pl_average' in result, f"'pl_average' not found in:\n{result}"
    assert '_orders.append' in result, f"'_orders.append' not found in:\n{result}"
    assert '_plots' in result, f"'_plots' not found in:\n{result}"
    assert 'value1 = 0' in result, f"'value1 = 0' not found in:\n{result}"
    assert 'condition1 = False' in result, f"'condition1 = False' not found in:\n{result}"


def test_complex_has_buy_and_sell():
    result = transpile(STRATEGY)
    assert "'buy'" in result, f"buy order not found in:\n{result}"
    assert "'sell'" in result, f"sell order not found in:\n{result}"


def test_complex_semantics():
    result = transpile(STRATEGY)
    # Provide a pl_average stub and execute
    ns = {
        'pl_average': lambda series, period: 50.0,
    }
    exec(result, ns)
    # close=60 > value1=50 => buy; market_position defaults to 0 => no sell
    out = ns['strategy'](open=55, high=65, low=45, close=60, volume=1000, date=0, time=0)
    assert any(o[0] == 'buy' for o in out['orders']), f"Expected buy order in {out['orders']}"
    assert len(out['plots'].get('plot1', [])) > 0, f"Expected plot1 data in {out['plots']}"


if __name__ == '__main__':
    import traceback
    fns = [v for k, v in list(globals().items()) if k.startswith('test_')]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS: {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
