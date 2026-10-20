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


def test_position_info_syntax():
    pl = "Value1 = EntryPrice; Value2 = PositionProfit; If BarsSinceEntry > 5 Then Sell Next Bar At Market;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_position_info_kwargs():
    pl = "Value1 = EntryPrice; Value2 = PositionProfit; If BarsSinceEntry > 5 Then Sell Next Bar At Market;"
    result = transpile(pl)
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"
    assert 'entry_price' in result, f"'entry_price' not found in:\n{result}"
    assert 'position_profit' in result, f"'position_profit' not found in:\n{result}"
    assert 'bars_since_entry' in result, f"'bars_since_entry' not found in:\n{result}"


def test_position_info_semantics():
    pl = "Value1 = EntryPrice; Value2 = PositionProfit; If BarsSinceEntry > 5 Then Sell Next Bar At Market;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0,
                          entry_price=50, position_profit=10, bars_since_entry=10)
    assert len(out['orders']) == 1, f"Expected 1 order, got {out['orders']}"
    assert out['orders'][0][0] == 'sell', f"Expected 'sell', got {out['orders'][0][0]}"


def test_position_info_no_sell_when_bars_low():
    pl = "Value1 = EntryPrice; Value2 = PositionProfit; If BarsSinceEntry > 5 Then Sell Next Bar At Market;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0,
                          entry_price=50, position_profit=10, bars_since_entry=2)
    assert len(out['orders']) == 0, f"Expected 0 orders, got {out['orders']}"


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
