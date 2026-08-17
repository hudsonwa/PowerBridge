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


def test_strlen_syntax():
    pl = 'Value1 = StrLen("hello");'
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_strlen_content():
    pl = 'Value1 = StrLen("hello");'
    result = transpile(pl)
    assert 'len(' in result, f"'len(' not found in:\n{result}"
    assert 'hello' in result, f"'hello' not found in:\n{result}"


def test_strlen_semantics():
    pl = 'Value1 = StrLen("hello");'
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    # We can't easily read value1 from the return, but we can verify it runs
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
    assert out is not None


def test_leftstr_syntax():
    pl = 'Value1 = LeftStr("hello", 3);'
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_leftstr_content():
    pl = 'Value1 = LeftStr("hello", 3);'
    result = transpile(pl)
    assert '[:3]' in result, f"'[:3]' not found in:\n{result}"


def test_rightstr_syntax():
    pl = 'Value1 = RightStr("hello", 3);'
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_rightstr_content():
    pl = 'Value1 = RightStr("hello", 3);'
    result = transpile(pl)
    assert '[-3:]' in result, f"'[-3:]' not found in:\n{result}"


def test_upperstr_syntax():
    pl = 'Value1 = UpperStr("hello");'
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_upperstr_content():
    pl = 'Value1 = UpperStr("hello");'
    result = transpile(pl)
    assert '.upper()' in result, f"'.upper()' not found in:\n{result}"


def test_lowerstr_syntax():
    pl = 'Value1 = LowerStr("HELLO");'
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_lowerstr_content():
    pl = 'Value1 = LowerStr("HELLO");'
    result = transpile(pl)
    assert '.lower()' in result, f"'.lower()' not found in:\n{result}"


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
