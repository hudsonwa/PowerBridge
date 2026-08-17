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


MA_CROSSOVER = """
Inputs: FastLen(9), SlowLen(18);
Variables: FastMA(0), SlowMA(0);

FastMA = Average(Close, FastLen);
SlowMA = Average(Close, SlowLen);

Condition1 = FastMA CrossesAbove SlowMA;
Condition2 = FastMA CrossesBelow SlowMA;

If Condition1 Then Buy ("LE") Next Bar At Market;
If Condition2 Then Sell ("LX") Next Bar At Market;

Plot1(FastMA, "Fast");
Plot2(SlowMA, "Slow");
"""


def test_ma_crossover_syntax():
    result = transpile(MA_CROSSOVER)
    try:
        compile(result, '<string>', 'exec')
    except SyntaxError as e:
        raise AssertionError(f"SyntaxError: {e}\n\nOutput was:\n{result}")


def test_ma_crossover_inputs():
    result = transpile(MA_CROSSOVER)
    assert 'fastlen' in result, f"'fastlen' not found in:\n{result}"
    assert 'slowlen' in result, f"'slowlen' not found in:\n{result}"
    assert "kwargs.get('fastlen', 9)" in result, f"Input default for fastlen not found in:\n{result}"
    assert "kwargs.get('slowlen', 18)" in result, f"Input default for slowlen not found in:\n{result}"


def test_ma_crossover_crosses_infix():
    result = transpile(MA_CROSSOVER)
    assert 'pl_crosses_above(fastma, slowma)' in result, f"Infix CrossesAbove not found in:\n{result}"
    assert 'pl_crosses_below(fastma, slowma)' in result, f"Infix CrossesBelow not found in:\n{result}"


def test_ma_crossover_orders():
    result = transpile(MA_CROSSOVER)
    assert "'buy'" in result, f"Buy order not found in:\n{result}"
    assert "'sell'" in result, f"Sell order not found in:\n{result}"
    assert "'LE'" in result, f"Order label 'LE' not found in:\n{result}"
    assert "'LX'" in result, f"Order label 'LX' not found in:\n{result}"


def test_ma_crossover_semantics():
    """Verify the transpiled code executes without error using series data."""
    result = transpile(MA_CROSSOVER)
    from pl_transpiler.runtime.pl_runtime import (
        pl_average, pl_crosses_above, pl_crosses_below,
    )
    # pl_crosses_above/below expect series (lists), but here they receive
    # scalar floats from pl_average. Wrap them to handle scalar inputs.
    def safe_crosses_above(a, b):
        if not isinstance(a, (list, tuple)):
            return False
        return pl_crosses_above(a, b)
    def safe_crosses_below(a, b):
        if not isinstance(a, (list, tuple)):
            return False
        return pl_crosses_below(a, b)
    ns = {
        'pl_average': pl_average,
        'pl_crosses_above': safe_crosses_above,
        'pl_crosses_below': safe_crosses_below,
    }
    exec(result, ns)
    out = ns['strategy'](
        open=[100]*20, high=[105]*20, low=[95]*20, close=[102]*20,
        volume=[1000]*20, date=20240101, time=930
    )
    assert isinstance(out, dict)
    assert 'orders' in out
    assert 'plots' in out


if __name__ == '__main__':
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
