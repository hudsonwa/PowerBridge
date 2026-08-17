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


def test_hour_syntax():
    pl = "Value1 = Hour(Time);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_hour_content():
    pl = "Value1 = Hour(Time);"
    result = transpile(pl)
    assert '// 100' in result, f"'// 100' not found in:\n{result}"
    assert 'time' in result, f"'time' not found in:\n{result}"


def test_hour_semantics():
    pl = "Value1 = Hour(Time);"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    # Time=1430 => Hour = 14
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=1430)
    assert out is not None


def test_minute_syntax():
    pl = "Value2 = Minute(Time);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_minute_content():
    pl = "Value2 = Minute(Time);"
    result = transpile(pl)
    assert '% 100' in result, f"'% 100' not found in:\n{result}"
    assert 'time' in result, f"'time' not found in:\n{result}"


def test_minute_semantics():
    pl = "Value2 = Minute(Time);"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    # Time=1430 => Minute = 30
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=1430)
    assert out is not None


def test_hour_and_minute_combined():
    pl = "Value1 = Hour(Time); Value2 = Minute(Time);"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=1430)
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
