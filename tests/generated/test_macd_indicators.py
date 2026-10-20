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


def test_macd_syntax():
    pl = "Value1 = MACD(Close, 12, 26);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_macd_content():
    pl = "Value1 = MACD(Close, 12, 26);"
    result = transpile(pl)
    assert 'pl_macd' in result, f"'pl_macd' not found in:\n{result}"
    assert 'close' in result, f"'close' not found in:\n{result}"
    assert '12' in result, f"'12' not found in:\n{result}"
    assert '26' in result, f"'26' not found in:\n{result}"


def test_macd_semantics():
    pl = "Value1 = MACD(Close, 12, 26);"
    result = transpile(pl)
    ns = {'pl_macd': lambda series, fast, slow: 1.5}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
    assert out is not None


def test_atr_syntax():
    pl = "Value1 = ATR(14);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_atr_content():
    pl = "Value1 = ATR(14);"
    result = transpile(pl)
    assert 'pl_atr' in result, f"'pl_atr' not found in:\n{result}"
    assert 'high' in result, f"'high' not found in:\n{result}"
    assert 'low' in result, f"'low' not found in:\n{result}"
    assert 'close' in result, f"'close' not found in:\n{result}"
    assert '14' in result, f"'14' not found in:\n{result}"


def test_atr_semantics():
    pl = "Value1 = ATR(14);"
    result = transpile(pl)
    ns = {'pl_atr': lambda h, l, c, period: 2.5}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
    assert out is not None


def test_roc_syntax():
    pl = "Value1 = ROC(Close, 14);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_roc_content():
    pl = "Value1 = ROC(Close, 14);"
    result = transpile(pl)
    assert 'pl_roc' in result, f"'pl_roc' not found in:\n{result}"
    assert 'close' in result, f"'close' not found in:\n{result}"
    assert '14' in result, f"'14' not found in:\n{result}"


def test_cci_syntax():
    pl = "Value1 = CCI(14);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_cci_content():
    pl = "Value1 = CCI(14);"
    result = transpile(pl)
    assert 'pl_cci' in result, f"'pl_cci' not found in:\n{result}"
    assert 'high' in result, f"'high' not found in:\n{result}"
    assert 'low' in result, f"'low' not found in:\n{result}"
    assert 'close' in result, f"'close' not found in:\n{result}"
    assert '14' in result, f"'14' not found in:\n{result}"


def test_cci_semantics():
    pl = "Value1 = CCI(14);"
    result = transpile(pl)
    ns = {'pl_cci': lambda h, l, c, period: 100.0}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
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
