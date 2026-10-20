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


def test_plot_syntax():
    pl = "Plot1(Close);"
    result = transpile(pl)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_plot_content():
    pl = "Plot1(Close);"
    result = transpile(pl)
    assert '_plots' in result, f"'_plots' not found in:\n{result}"
    assert 'plot1' in result, f"'plot1' not found in:\n{result}"


def test_alert_content():
    pl = 'Alert("Signal fired");'
    result = transpile(pl)
    assert '_alerts.append' in result, f"'_alerts.append' not found in:\n{result}"
    assert 'Signal fired' in result, f"'Signal fired' not found in:\n{result}"


def test_print_content():
    pl = 'Print("Hello");'
    result = transpile(pl)
    assert 'print(' in result, f"'print(' not found in:\n{result}"
    assert 'Hello' in result, f"'Hello' not found in:\n{result}"


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
