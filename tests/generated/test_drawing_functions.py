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


def test_tl_new_syntax():
    pl = "Value1 = TL_New(Date, Time, High, Date, Time, Low);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_tl_new_content():
    pl = "Value1 = TL_New(Date, Time, High, Date, Time, Low);"
    result = transpile(pl)
    assert 'pl_tl_new' in result, f"'pl_tl_new' not found in:\n{result}"
    assert 'date' in result, f"'date' not found in:\n{result}"
    assert 'time' in result, f"'time' not found in:\n{result}"
    assert 'high' in result, f"'high' not found in:\n{result}"
    assert 'low' in result, f"'low' not found in:\n{result}"


def test_tl_new_semantics():
    pl = "Value1 = TL_New(Date, Time, High, Date, Time, Low);"
    result = transpile(pl)
    ns = {'pl_tl_new': lambda *args: 1}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=20260101, time=930)
    assert out is not None


def test_text_new_syntax():
    pl = 'Text_New(Date, Time, High, "Label");'
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_text_new_content():
    pl = 'Text_New(Date, Time, High, "Label");'
    result = transpile(pl)
    assert 'pl_text_new' in result, f"'pl_text_new' not found in:\n{result}"
    assert 'Label' in result, f"'Label' not found in:\n{result}"


def test_text_new_semantics():
    pl = 'Text_New(Date, Time, High, "Label");'
    result = transpile(pl)
    ns = {'pl_text_new': lambda *args: 1}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=20260101, time=930)
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
