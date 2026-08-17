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


def test_average_syntax():
    pl = "Value1 = Average(Close, 14);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_average_content():
    pl = "Value1 = Average(Close, 14);"
    result = transpile(pl)
    assert 'pl_average' in result, f"'pl_average' not found in:\n{result}"
    assert 'close' in result, f"'close' not found in:\n{result}"
    assert '14' in result, f"'14' not found in:\n{result}"


def test_highest_content():
    pl = "Value1 = Highest(High, 20);"
    result = transpile(pl)
    assert 'pl_highest' in result, f"'pl_highest' not found in:\n{result}"


def test_lowest_content():
    pl = "Value1 = Lowest(Low, 20);"
    result = transpile(pl)
    assert 'pl_lowest' in result, f"'pl_lowest' not found in:\n{result}"


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
