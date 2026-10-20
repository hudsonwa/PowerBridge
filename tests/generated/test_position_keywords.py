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


def test_netprofit_syntax():
    pl = "Value1 = NetProfit;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_netprofit_content():
    pl = "Value1 = NetProfit;"
    result = transpile(pl)
    assert 'net_profit' in result, f"'net_profit' not found in:\n{result}"
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"


def test_netprofit_semantics():
    pl = "Value1 = NetProfit;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0, net_profit=5000)
    assert out is not None


def test_grossprofit_syntax():
    pl = "Value2 = GrossProfit;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_grossprofit_content():
    pl = "Value2 = GrossProfit;"
    result = transpile(pl)
    assert 'gross_profit' in result, f"'gross_profit' not found in:\n{result}"
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"


def test_entrydate_syntax():
    pl = "Value3 = EntryDate;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_entrydate_content():
    pl = "Value3 = EntryDate;"
    result = transpile(pl)
    assert 'entry_date' in result, f"'entry_date' not found in:\n{result}"
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"


def test_maxpositionprofit_syntax():
    pl = "Value4 = MaxPositionProfit;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_maxpositionprofit_content():
    pl = "Value4 = MaxPositionProfit;"
    result = transpile(pl)
    assert 'max_position_profit' in result, f"'max_position_profit' not found in:\n{result}"
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"


def test_position_keywords_combined_semantics():
    pl = "Value1 = NetProfit; Value2 = GrossProfit; Value3 = EntryDate; Value4 = MaxPositionProfit;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0,
                          net_profit=5000, gross_profit=8000, entry_date=20260101,
                          max_position_profit=1200)
    assert out is not None


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
