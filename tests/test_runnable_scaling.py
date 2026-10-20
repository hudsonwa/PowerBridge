"""Default (persist) ``pl_transpile`` output runs in time linear in bar count.

Lookback vars passed to series functions are emitted as ``(_state['x'] + [x])``.
On plain lists that copies the whole history every bar, so the README's
``run.py`` loop was O(bars^2). The runnable output now seeds ``_state`` as a
``SeriesState`` (as ``pl_run``'s bar loop does), which makes that an O(1) view.
A caller-supplied plain-dict ``_state`` still works and serves as the reference.
"""
import os
import random
import time

import pytest

from pl_transpiler import transpile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Lookback reads, a same-bar reassignment, and series-function args.
ALIAS_EL = """
Inputs: Len(5);
Vars: x(0), y(0), z(0);
x = Close - Close[1];
x = x * 0.5 + x[1] * 0.5;
y = Average(x, Len) + XAverage(x, 3);
z = Highest(y, 4) - y[2];
if x crosses above y then Buy next bar at market;
if x crosses below y or z[1] > z then Sell next bar at market;
"""

SOURCES = {
    "ma_cross": os.path.join(REPO, "examples", "ma_cross.el"),
    "gt2": os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt"),
}


def _source(name):
    if name == "alias":
        return ALIAS_EL
    with open(SOURCES[name], encoding="utf-8") as f:
        return f.read()


def _strategy(src):
    g = {}
    exec(compile(transpile(src, persist=True), "<persist>", "exec"), g)
    return g["strategy"]


def _bars(n, seed=7):
    rng = random.Random(seed)
    price, minute, day = 15000.0, 9 * 60 + 30, 1240102
    for _ in range(n):
        o = price
        c = round((o + rng.gauss(0, 6)) * 4) / 4
        h = max(o, c) + rng.randint(0, 8) / 4
        l = min(o, c) - rng.randint(0, 8) / 4
        yield o, h, l, c, float(rng.randint(100, 5000)), day, minute // 60 * 100 + minute % 60
        price, minute = c, minute + 5
        if minute >= 16 * 60:
            minute, day = 9 * 60 + 30, day + 1


def _run(strategy, n, kwargs=None):
    """Drive ``strategy`` the way the README's run.py does; per-bar outputs."""
    cols = [[] for _ in range(7)]
    kwargs = {} if kwargs is None else kwargs
    out = []
    for bar in _bars(n):
        for col, v in zip(cols, bar):
            col.append(v)
        r = strategy(*cols, kwargs)
        out.append((r["orders"], r["plots"]))
    return out


def test_default_output_uses_series_state():
    src = transpile(_source("ma_cross"), persist=True)
    assert "_PbSeriesState()" in src
    # Trace output (pl_run's dialect) is unchanged; its loop supplies _state.
    assert "_PbSeriesState" not in transpile(_source("ma_cross"), trace=True, persist=True)


@pytest.mark.parametrize("name", ["ma_cross", "gt2", "alias"])
def test_matches_plain_list_reference(name):
    strategy = _strategy(_source(name))
    got = _run(strategy, 2000)
    ref = _run(strategy, 2000, kwargs={"_state": {}})
    assert got == ref
    assert any(orders for orders, _ in got)


def _best(strategy, n, reps):
    best = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        _run(strategy, n)
        best = min(best, time.perf_counter() - t)
    return best


def test_run_time_is_linear_in_bars():
    strategy = _strategy(_source("ma_cross"))
    small = _best(strategy, 5000, 3)
    large = _best(strategy, 20000, 1)
    # 4x the bars: ~4x when linear, ~16x when quadratic.
    assert large / small < 8, (small, large)
