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


def test_setplotcolor_syntax():
    pl = "SetPlotColor(1, 2);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_setplotcolor_content():
    pl = "SetPlotColor(1, 2);"
    result = transpile(pl)
    assert '_plots' in result, f"'_plots' not found in:\n{result}"
    assert 'color' in result, f"'color' not found in:\n{result}"


def test_setplotcolor_semantics():
    pl = "SetPlotColor(1, 2);"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
    assert 'plot1_color' in out['plots'], f"'plot1_color' not found in plots: {out['plots']}"
    assert out['plots']['plot1_color'] == 2, f"Expected color=2, got {out['plots']['plot1_color']}"


def test_setplotwidth_syntax():
    pl = "SetPlotWidth(1, 3);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_setplotwidth_content():
    pl = "SetPlotWidth(1, 3);"
    result = transpile(pl)
    assert '_plots' in result, f"'_plots' not found in:\n{result}"
    assert 'width' in result, f"'width' not found in:\n{result}"


def test_setplotwidth_semantics():
    pl = "SetPlotWidth(1, 3);"
    result = transpile(pl)
    ns = {}
    exec(result, ns)
    out = ns['strategy'](open=10, high=15, low=5, close=12, volume=100, date=0, time=0)
    assert 'plot1_width' in out['plots'], f"'plot1_width' not found in plots: {out['plots']}"
    assert out['plots']['plot1_width'] == 3, f"Expected width=3, got {out['plots']['plot1_width']}"


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
