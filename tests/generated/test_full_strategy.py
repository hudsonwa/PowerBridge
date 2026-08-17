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
Inputs: Length(14), Multiplier(2);
Variables: Value1(0), Value2(0), Condition1(False);
Value1 = Average(Close, Length);
Value2 = StandardDev(Close, Length);
Condition1 = Close > Value1 + Multiplier * Value2;
If Condition1 And MarketPosition <> 1 Then
  Buy Next Bar At Market;
If MarketPosition = 1 And Close < Value1 Then
  Sell Next Bar At Market;
SetStopLoss(500);
SetProfitTarget(1000);
Plot1(Value1, "MA");
Plot2(Value1 + Multiplier * Value2, "Upper");
Plot3(Value1 - Multiplier * Value2, "Lower");
"""


def test_full_strategy_syntax():
    result = transpile(STRATEGY)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_full_strategy_content_inputs():
    result = transpile(STRATEGY)
    assert "kwargs.get('length', 14)" in result, f"Input 'length' not found in:\n{result}"
    assert "kwargs.get('multiplier', 2)" in result, f"Input 'multiplier' not found in:\n{result}"


def test_full_strategy_content_variables():
    result = transpile(STRATEGY)
    assert 'value1 = 0' in result, f"'value1 = 0' not found in:\n{result}"
    assert 'value2 = 0' in result, f"'value2 = 0' not found in:\n{result}"
    assert 'condition1 = False' in result, f"'condition1 = False' not found in:\n{result}"


def test_full_strategy_content_indicators():
    result = transpile(STRATEGY)
    assert 'pl_average' in result, f"'pl_average' not found in:\n{result}"
    assert 'pl_std_dev' in result, f"'pl_std_dev' not found in:\n{result}"


def test_full_strategy_content_orders():
    result = transpile(STRATEGY)
    assert "'buy'" in result, f"buy order not found in:\n{result}"
    assert "'sell'" in result, f"sell order not found in:\n{result}"
    assert '_market_position' in result, f"'_market_position' not found in:\n{result}"


def test_full_strategy_content_risk():
    result = transpile(STRATEGY)
    assert "_risk['stop_loss'] = 500" in result, f"stop_loss not found in:\n{result}"
    assert "_risk['profit_target'] = 1000" in result, f"profit_target not found in:\n{result}"


def test_full_strategy_content_plots():
    result = transpile(STRATEGY)
    assert 'plot1' in result, f"'plot1' not found in:\n{result}"
    assert 'plot2' in result, f"'plot2' not found in:\n{result}"
    assert 'plot3' in result, f"'plot3' not found in:\n{result}"
    assert "'MA'" in result, f"plot label 'MA' not found in:\n{result}"
    assert "'Upper'" in result, f"plot label 'Upper' not found in:\n{result}"
    assert "'Lower'" in result, f"plot label 'Lower' not found in:\n{result}"


def test_full_strategy_semantics_buy():
    result = transpile(STRATEGY)
    ns = {
        'pl_average': lambda series, period: 50.0,
        'pl_std_dev': lambda series, period: 5.0,
    }
    exec(result, ns)
    # close=65 > value1(50) + multiplier(2)*value2(5) = 60 => condition1=True
    # market_position defaults to 0, so <> 1 => buy
    out = ns['strategy'](open=60, high=70, low=55, close=65, volume=1000, date=0, time=0)
    buy_orders = [o for o in out['orders'] if o[0] == 'buy']
    assert len(buy_orders) == 1, f"Expected 1 buy order, got {buy_orders}"
    # No sell since market_position=0
    sell_orders = [o for o in out['orders'] if o[0] == 'sell']
    assert len(sell_orders) == 0, f"Expected 0 sell orders, got {sell_orders}"


def test_full_strategy_semantics_sell():
    result = transpile(STRATEGY)
    ns = {
        'pl_average': lambda series, period: 50.0,
        'pl_std_dev': lambda series, period: 5.0,
    }
    exec(result, ns)
    # close=45 < value1(50) => sell condition met
    # market_position=1 => sell
    out = ns['strategy'](open=48, high=50, low=44, close=45, volume=1000, date=0, time=0,
                          market_position=1)
    sell_orders = [o for o in out['orders'] if o[0] == 'sell']
    assert len(sell_orders) == 1, f"Expected 1 sell order, got {sell_orders}"


def test_full_strategy_semantics_risk():
    result = transpile(STRATEGY)
    ns = {
        'pl_average': lambda series, period: 50.0,
        'pl_std_dev': lambda series, period: 5.0,
    }
    exec(result, ns)
    out = ns['strategy'](open=50, high=55, low=45, close=50, volume=1000, date=0, time=0)
    assert out['risk']['stop_loss'] == 500, f"Expected stop_loss=500, got {out['risk']}"
    assert out['risk']['profit_target'] == 1000, f"Expected profit_target=1000, got {out['risk']}"


def test_full_strategy_semantics_plots():
    result = transpile(STRATEGY)
    ns = {
        'pl_average': lambda series, period: 50.0,
        'pl_std_dev': lambda series, period: 5.0,
    }
    exec(result, ns)
    out = ns['strategy'](open=50, high=55, low=45, close=50, volume=1000, date=0, time=0)
    assert 'plot1' in out['plots'], f"plot1 not in plots: {out['plots']}"
    assert 'plot2' in out['plots'], f"plot2 not in plots: {out['plots']}"
    assert 'plot3' in out['plots'], f"plot3 not in plots: {out['plots']}"
    # plot1 = value1 = 50.0
    assert out['plots']['plot1'] == [50.0], f"Expected plot1=[50.0], got {out['plots']['plot1']}"
    # plot2 = value1 + multiplier*value2 = 50 + 2*5 = 60
    assert out['plots']['plot2'] == [60.0], f"Expected plot2=[60.0], got {out['plots']['plot2']}"
    # plot3 = value1 - multiplier*value2 = 50 - 2*5 = 40
    assert out['plots']['plot3'] == [40.0], f"Expected plot3=[40.0], got {out['plots']['plot3']}"


def test_full_strategy_semantics_custom_inputs():
    result = transpile(STRATEGY)
    ns = {
        'pl_average': lambda series, period: 100.0,
        'pl_std_dev': lambda series, period: 10.0,
    }
    exec(result, ns)
    # Override Length=20, Multiplier=3 via kwargs
    out = ns['strategy'](open=50, high=55, low=45, close=50, volume=1000, date=0, time=0,
                          length=20, multiplier=3)
    # plot3 = value1 - multiplier*value2 = 100 - 3*10 = 70
    assert out['plots']['plot3'] == [70.0], f"Expected plot3=[70.0], got {out['plots']['plot3']}"


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
