"""Crossing a constant level: ``X crosses above 0`` must fire.

The crosses runtime skips leading zeros (the runner's PAD element and indicator
warm-up). A side that is all zeros, such as a declared ``Z(0)`` or the literal
``0``, used to be skipped entirely, so the cross never started.
"""

from pathlib import Path

import pytest

from pl_transpiler import get_config, load_bars, run_oracle
from pl_transpiler.runtime._rt_indicators import pl_crosses_above, pl_crosses_below

BARS = Path(__file__).resolve().parent.parent / 'examples' / 'NQ_sample_bars.csv'

HEAD = 'Vars: X(0), Z(0);\nX = Close - Average(Close, 20);\n'
# CurrentBar > 25 keeps the comparison past the warm-up, where the explicit
# form would read X[1] from the zero pad on the first bar.
CROSS = {
    'literal': ('X crosses above 0', 'X crosses below 0'),
    'declared': ('X crosses above Z', 'X crosses below Z'),
    'constant': ('X crosses above 5', 'X crosses below 5'),
}
EXPLICIT = {
    'literal': ('X > 0 and X[1] <= 0', 'X < 0 and X[1] >= 0'),
    'declared': ('X > 0 and X[1] <= 0', 'X < 0 and X[1] >= 0'),
    'constant': ('X > 5 and X[1] <= 5', 'X < 5 and X[1] >= 5'),
}


def _strategy(up, down):
    return (HEAD
            + f'If CurrentBar > 25 and {up} then Buy next bar at market;\n'
            + f'If CurrentBar > 25 and {down} then Sell next bar at market;\n')


def _digest(src, native):
    return run_oracle(src, load_bars(str(BARS)), get_config('NQ'), use_native=native)


@pytest.mark.parametrize('kind', sorted(CROSS))
def test_crosses_constant_matches_explicit_python(kind):
    got = _digest(_strategy(*CROSS[kind]), False)
    want = _digest(_strategy(*EXPLICIT[kind]), False)
    assert got['fills'] > 0
    assert got['sha256'] == want['sha256']


@pytest.mark.parametrize('kind', sorted(CROSS))
def test_crosses_constant_matches_explicit_native(kind):
    pytest.importorskip('pl_transpiler._accel')
    src = _strategy(*CROSS[kind])
    got = _digest(src, True)
    assert got['sha256'] == _digest(_strategy(*EXPLICIT[kind]), False)['sha256']
    assert got['sha256'] == _digest(src, False)['sha256']


def test_runtime_zero_level():
    x = [0.0, 5.0, -1.0, -2.0, 3.0]
    zeros = [0.0] * len(x)
    assert pl_crosses_above(x, zeros)
    assert pl_crosses_above(x, 0)
    assert not pl_crosses_below(x, 0)
    assert pl_crosses_below(x[:3], zeros[:3])
    assert pl_crosses_below(x[:3], 0.0)
    # The leading pad zero on X is still skipped: no cross on its first bar.
    assert not pl_crosses_above(x[:2], zeros[:2])
    # Two all-zero sides never cross.
    assert not pl_crosses_above(zeros, zeros)


def test_runtime_warmup_skip_kept():
    # A warm-up side (zeros, then values) still starts at its first value.
    fast = [0.0, 10.0, 11.0, 12.0]
    slow = [0.0, 0.0, 0.0, 11.5]
    assert not pl_crosses_above(fast, slow)
    assert not pl_crosses_below(fast, slow)
