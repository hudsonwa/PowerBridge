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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
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
    # Infix crosses compare the variables' bar history, not two scalars.
    assert "pl_crosses_above((_state['fastma'] + [fastma]), (_state['slowma'] + [slowma]))" in result, f"Infix CrossesAbove not found in:\n{result}"
    assert "pl_crosses_below((_state['fastma'] + [fastma]), (_state['slowma'] + [slowma]))" in result, f"Infix CrossesBelow not found in:\n{result}"


def test_ma_crossover_orders():
    result = transpile(MA_CROSSOVER)
    assert "'buy'" in result, f"Buy order not found in:\n{result}"
    assert "'sell'" in result, f"Sell order not found in:\n{result}"
    assert "'LE'" in result, f"Order label 'LE' not found in:\n{result}"
    assert "'LX'" in result, f"Order label 'LX' not found in:\n{result}"


def test_ma_crossover_semantics():
    """Run the runnable (persist) dialect bar by bar on a rising-then-falling series."""
    result = transpile(MA_CROSSOVER, persist=True)
    ns = {}
    exec(compile(result, '<ma-crossover>', 'exec'), ns)
    closes = [100.0 + i for i in range(30)] + [130.0 - i for i in range(30)]
    state = {}
    sides = []
    for i in range(1, len(closes) + 1):
        c = closes[:i]
        out = ns['strategy'](c, c, c, c, [1000] * i, [20240101] * i, [930] * i,
                             _state=state, barnumber=i)
        assert isinstance(out, dict)
        assert 'orders' in out and 'plots' in out
        sides += [o[0] for o in out['orders']]
    assert 'sell' in sides, f"expected a CrossesBelow exit, got {sides}"


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
