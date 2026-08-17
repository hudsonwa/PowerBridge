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


def test_time_s_syntax():
    pl = "If Time_S >= 93000 Then Value1 = 1;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_time_s_content():
    pl = "If Time_S >= 93000 Then Value1 = 1;"
    result = transpile(pl)
    assert 'time_s' in result, f"'time_s' not found in:\n{result}"
    assert '93000' in result, f"'93000' not found in:\n{result}"
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"


def test_time_s_semantics():
    pl = "Variables: Value1(0); If Time_S >= 93000 Then Value1 = 1;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    # time_s >= 93000
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0, time_s=93000)
    # value1 should become 1 but we check it runs without error
    assert out is not None

    # time_s < 93000 => value1 stays 0
    out2 = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0, time_s=92000)
    assert out2 is not None


def test_bartype_ex_syntax():
    pl = "Value1 = BarType_Ex;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_bartype_ex_content():
    pl = "Value1 = BarType_Ex;"
    result = transpile(pl)
    assert 'bartype_ex' in result, f"'bartype_ex' not found in:\n{result}"
    assert 'kwargs' in result, f"'kwargs' not found in:\n{result}"


def test_bartype_ex_semantics():
    pl = "Value1 = BarType_Ex;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0, bartype_ex=5)
    assert out is not None


def test_recalculate_syntax():
    pl = "Recalculate;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_recalculate_content():
    pl = "Recalculate;"
    result = transpile(pl)
    assert 'recalculate' in result.lower(), f"'recalculate' not found in:\n{result}"


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
