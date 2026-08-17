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


def test_switch_case_syntax():
    pl = "Switch (Value1) Begin Case 1: Value2 = 10; Case 2: Value2 = 20; Default: Value2 = 0; End;"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_switch_case_contains_if_elif_else():
    pl = "Switch (Value1) Begin Case 1: Value2 = 10; Case 2: Value2 = 20; Default: Value2 = 0; End;"
    result = transpile(pl)
    assert 'if ' in result, f"'if' not found in:\n{result}"
    assert 'elif ' in result, f"'elif' not found in:\n{result}"
    assert 'else' in result, f"'else' not found in:\n{result}"


def test_switch_case_values():
    pl = "Switch (Value1) Begin Case 1: Value2 = 10; Case 2: Value2 = 20; Default: Value2 = 0; End;"
    result = transpile(pl)
    assert 'value2 = 10' in result, f"'value2 = 10' not found in:\n{result}"
    assert 'value2 = 20' in result, f"'value2 = 20' not found in:\n{result}"
    assert 'value2 = 0' in result, f"'value2 = 0' not found in:\n{result}"


def test_switch_case_semantics():
    pl = "Value1 = 2; Switch (Value1) Begin Case 1: Value2 = 10; Case 2: Value2 = 20; Default: Value2 = 0; End;"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
    assert isinstance(out, dict), f"Expected dict, got {type(out)}"


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
