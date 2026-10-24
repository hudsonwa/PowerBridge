# Writing a strategy in Python

`el_emit` turns Python into EasyLanguage, but only Python written in the
**mirror dialect**: the small, fixed shape that `pl_transpile --no-persist`
itself produces. It is not general Python. Anything outside the dialect is
rejected with an error; nothing is guessed. This page teaches the dialect from
scratch. [LLM_DIALECT.md](LLM_DIALECT.md) lists every accepted construct.

What works today:

- Write the strategy in the dialect, run `el_emit` to get EasyLanguage plus a
  check that every builtin is known to compile in MultiCharts.
- Backtest the **emitted EasyLanguage** with `pl_oracle` or `pl_run`. The
  mirror-dialect `.py` file itself is not runnable as a backtest; it is the
  source you edit.

The worked example is [examples/hand_written.py](../examples/hand_written.py),
and `tests/test_hand_written_example.py` checks everything on this page.

## The skeleton

<!-- readme-test: skip (fragment of a strategy) -->
```python
from pl_transpiler.runtime.pl_runtime import *


def strategy(open, high, low, close, volume, date, time, **kwargs):
    # 1. inputs   2. variables   3. body
    return {'orders': _orders, 'plots': _plots, 'alerts': _alerts, 'risk': _risk}
```

There must be exactly one function, named `strategy`, with exactly those
parameters. The import line and the `return` are fixed boilerplate. Names such
as `_orders`, `_risk` and `_market_position` are provided; you do not create
them.

## Inputs

One line per input, in order, before anything else:

<!-- readme-test: skip (fragment of a strategy) -->
```python
trendlen = kwargs.get('trendlen', 20)
```

The key must equal the variable name. This becomes `Inputs: trendlen( 20 ) ;`.

## Variables

Next, declare each variable once, with its initial value:

<!-- readme-test: skip (fragment of a strategy) -->
```python
trend = f32(0)        # Vars: trend( 0 ) ;
label = "none"        # Vars: label( "none" ) ;
levels = [0] * (5 + 1) # Arrays: levels[5]( 0 ) ;
```

Declarations form one block before the first statement. A name assigned
for the first time after the body starts is treated as an undeclared
identifier, and `el_emit`'s check reports it as `NOT-PROVEN`.

## Series and lookbacks

`open`, `high`, `low`, `close`, `volume`, `date` and `time` are series.
The current bar is `[-1]`; *n* bars ago is `[-(n + 1)]`:

| Python | EasyLanguage |
|---|---|
| `close[-1]` | `close` |
| `close[-(1 + 1)]` | `close[1]` |
| `close[-(dips + 1)]` | `close[dips]` |

Built-in indicators are `pl_*` helpers that take the series and a length,
for example `pl_average(close, 20)` → `average(close, 20)` and
`pl_highest(high, 5)` → `highest(high, 5)`. (The optional Rust kernel does
not run every helper yet; `Highest`, for one, falls back to the Python engine
with a stderr note.) Run `pl_transpile --no-persist`
on a line of EasyLanguage to see the helper name for any builtin.

**Warm-up.** MultiCharts skips its MaxBarsBack bars before evaluating a
strategy. *New in the next release:* `pl_oracle` / `pl_run` do the same by
default (`--max-bars-back auto`, the longest lookback found in the source).
With `--max-bars-back 0` (the 0.10.0 default) every bar is evaluated from
bar 1, so `average(close, 20)` returns 0 for the first 19 bars and `close[5]`
on bar 1 reads that bar's open; see [LIMITATIONS.md](LIMITATIONS.md).

## Orders and risk

An order is a tuple appended to `_orders`:

<!-- readme-test: skip (fragment of a strategy) -->
```python
_orders.append(('buy', 'market', 1, 'next', 'DipLE'))              # Buy ("DipLE") 1 contract next bar at market
_orders.append(('sell', 'limit', 0, 'next', 'TargetLX', exitlevel)) # Sell ("TargetLX") next bar at exitlevel limit
_orders.append(('sellshort', 'stop', 0, 'next', 'SE', lo))          # SellShort ("SE") next bar at lo stop
```

The fields are action (`buy`, `sell`, `sellshort`, `buytocover`), order type
(`market`, `limit`, `stop`), quantity (0 means the default size), `next`
(next bar), the signal name, and the price for limit and stop orders.
The current position is `_market_position` (1 long, -1 short, 0 flat).

Stops and targets are set through `_risk`, for example
`_risk['stop_loss'] = stopusd` becomes `SetStopLoss( stopusd ) ;`
(a dollar amount per position, as in EasyLanguage).

Conditions use `if` / `else` with `and`, `or`, `not`; loops use
`for i in range(int(a), int(b) + 1):` or `while`.

## Run it

```bash
el_emit examples/hand_written.py -o hand_written.el
```

```
hand_written.el: 5 builtins used | 5 PROVEN | 0 NOT-PROVEN | 0 ARITY-BAD | 0 RETURN-TYPE-BAD | 0 ARG-TYPE-BAD
OK -- every builtin used is known to compile in MultiCharts (names, argument counts, return types).
...
```

```bash
pl_oracle hand_written.el examples/NQ_sample_bars.csv
```

```
powerbridge-trade-digest v2
fills: 6
round_turns: 3
sha256: 3537d0ba687746fd8846ec2127d8b472b75e1328b257949925e0a04d4fceb4c0
```

This strategy reads up to 20 past bars, so the default `--max-bars-back auto`
skips the first 20 (stderr says `max-bars-back 20 (auto)`). *New in the next
release;* on 0.10.0, or with `--max-bars-back 0`, the digest is
`9f11e024f4832be65e4c22641085e47c7ec731357162a801a885de95663f61aa` and stderr
shows one `warning: lookback past available history` line. The digest is the same on the Rust and Python
paths, and the emitted EasyLanguage survives a round trip:
`pl_transpile --no-persist` on it and `el_emit` again gives the same
statements (blank lines are not kept).

## What el_emit rejects

Input outside the dialect stops with one line on stderr and exit code 2:

```
el_emit: error: unsupported function sum(...)
```

That one comes from `x = sum(close[-5:]) / 5`; write `pl_average(close, 5)`
instead. Other common errors:

| You wrote | Error |
|---|---|
| `n = kwargs.get('Len', 3)` | `unrecognised kwargs.get key 'Len'` (key must equal the name) |
| `a < b < c` | `chained comparisons are outside the dialect` |
| a `try` block, a `with` block | `unsupported statement: ...` with the line number |
| a list comprehension, a lambda | `cannot invert expression: ...` |
| a second `def` | `more than one function definition` |

Code that parses but uses an unknown name (for example a variable first
assigned in the body) emits EasyLanguage, then the check prints
`NOT-PROVEN` with the name and `el_emit` exits 1.
