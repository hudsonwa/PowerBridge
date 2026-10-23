<!--
 Copyright 2026 Joshua Hudson

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
-->

# Usage guide

The [README](../README.md#quickstart) has the short quickstart. This
page goes through the same example step by step, then covers the Python to
EasyLanguage direction, the library API, your own bar data and other
instruments. Every `bash` and `python` block here is run by
`tests/test_readme_examples.py`, from the repository root, after the README's.

## Status and scope

**Beta.** Trade lists match MultiCharts fill-for-fill on the 1-minute @NQ
ground-truth suite and on 1-minute @ES (four configurations of a multi-entry
strategy, 4,278 of 4,278 fills identical). The optional Rust kernel runs the
same backtest about 5x faster with identical fills
([measured](ACCEL.md#speed) with `tools/bench_native.py`).

Two directions are supported. Take existing EL and run it bar by bar in
Python. Or write a strategy in PowerBridge's small Python "mirror dialect"
(not general Python) and emit EasyLanguage that passes name, argument-count and
return-type checks for MultiCharts or TradeStation, then backtest that EL here.
The import name is `pl_transpiler`. Everything is the Python standard library:
no PyPI deps, no network.

It would rather stop than guess. Unsupported keywords are reported in one pass
(see [Limitations](LIMITATIONS.md)).

## Walkthrough

Python 3.9 or newer, no other dependencies. Run these from the repository root.

**1. Install** (a virtual environment is recommended):

<!-- readme-test: skip (creates a venv and installs) -->
```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -U pip
pip install .
```

That installs the `powerbridge` distribution (import name `pl_transpiler`) and
five commands: `pl_transpile`, `pl_run`, `pl_oracle`, `el_emit` and
`pl_examples` (writes the quickstart's example files into `./examples/`).

**2. Transpile an example** to Python. The examples below write their files
into `out/`, which git and ruff ignore:

```bash
mkdir -p out
pl_transpile examples/ma_cross.el -o out/ma_cross.py
```

```
[pl_transpile] wrote out/ma_cross.py
```

`examples/ma_cross.el`, the strategy used throughout:

```
Inputs: FastLen(9), SlowLen(21);
Vars: Fast(0), Slow(0);
Fast = Average(Close, FastLen);
Slow = Average(Close, SlowLen);
If Fast crosses above Slow Then Buy ("LE") Next Bar At Market;
If Fast crosses below Slow Then Sell ("LX") Next Bar At Market;
```

The generated Python is plain code (excerpt):

<!-- readme-test: skip (excerpt of generated code) -->
```python
fast = f32(pl_average(close, fastlen))
slow = f32(pl_average(close, slowlen))
if pl_crosses_above((_state['fast'] + [fast]), (_state['slow'] + [slow])):
    _orders.append(('buy', 'market', 0, 'next', 'LE'))
```

`out/ma_cross.py` is runnable: it defines `strategy(open, high, low, close, volume,
date, time, kwargs)`, takes the bar series so far, and keeps its cross-bar
state (`Vars`, lookback, `Inputs`) in `kwargs['_state']`. Pass the same
`kwargs` dict on every bar. To see its order signals, save this as `out/run.py`
and run `python out/run.py` from the repository root:

```python
import csv
import sys

sys.path.insert(0, "out")
from ma_cross import strategy  # noqa: E402

cols = {k: [] for k in ("open", "high", "low", "close", "volume", "date", "time")}
kwargs = {}  # strategy keeps its cross-bar state in kwargs['_state']
with open("examples/NQ_sample_bars.csv") as f:
    for row in csv.DictReader(f):
        for k in ("open", "high", "low", "close", "volume"):
            cols[k].append(float(row[k]))
        cols["date"].append(int(row["date"]))
        cols["time"].append(int(row["time"]))
        out = strategy(*cols.values(), kwargs)
        for order in out["orders"]:
            print(row["date"], row["time"], order)
```

```
1240102 954 ('sell', 'market', 0, 'next', 'LX')
1240102 1029 ('buy', 'market', 0, 'next', 'LE')
1240102 1104 ('sell', 'market', 0, 'next', 'LX')
1240102 1140 ('buy', 'market', 0, 'next', 'LE')
```

These are raw signals: the first `LX` arrives while flat and fills nothing.
For fills, positions and P&L use `pl_run` or `pl_oracle` below.
`pl_transpile --no-persist` emits the clean mirror dialect for `el_emit`
instead; that file is not runnable on its own.

**3. Run a backtest** on the bundled synthetic bars. `pl_run` executes the
generated Python on your machine, so only run files you trust (transpiling
alone executes nothing).

```bash
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv \
    --config NQ --columns MarketPosition,NetProfit,TotalTrades -o out/run_out.csv
```

```
[pl_run] 140 bars, 140 rows, 3 fills, 1 round-turns [python] -> out/run_out.csv
```

That status line goes to stderr. With the optional
[Rust fast path](ACCEL.md#install) installed it says `[native]` instead of
`[python]`; the CSV is byte-identical either way. Add `-v` to see a `note:`
line on stderr when the fast path is not installed.

**4. Read the output.** `out/run_out.csv` has one row per bar with the columns you
asked for. The strategy goes long, exits with a
profit, and is long again at the end (excerpt):

<!-- readme-test: file out/run_out.csv -->
```
MarketPosition,NetProfit,TotalTrades
0,0.00,0
...
1,0.00,0
...
0,1343.80,1
...
1,1343.80,1
```

Money columns are rounded to cents and price columns to the instrument's tick
(`--full-precision` writes the raw floats). Column names are the run's trace keys; omit `--columns` to get all of them in
the header. EasyLanguage spellings such as `MarketPosition`, `NetProfit` or
`CurrentBar` are accepted case-insensitively. An unknown name stops the run
with exit code 2 and suggests the closest keys.

For a compact, reproducible summary of the trade list, use `pl_oracle`:

```bash
pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv
```

```
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

The digest goes to stdout. The `[pl_oracle] … fills` summary line (and,
with `-v`, a `note:` line when the native kernel is not installed) go to stderr, so
`pl_oracle … > digest.txt` captures only the block above.

To see why a strategy would run on the Python path, list every construct the
native kernel refuses (no bars CSV needed, nothing runs):
`pl_oracle --explain-native my_strategy.el`. It is a compile-time check: a run
can still be refused at run time. Details are in
[ACCEL.md](ACCEL.md#which-constructs-the-kernel-refuses).

What you can verify yourself, digest gating and captures are in
[verification.md](verification.md).

## Python (mirror dialect) to EasyLanguage

The round trip on the example: transpile to the mirror dialect, then emit
checked EasyLanguage:

```bash
mkdir -p out
pl_transpile examples/ma_cross.el --no-persist -o out/ma_mirror.py
```

```
[pl_transpile] wrote out/ma_mirror.py
```

```bash
el_emit out/ma_mirror.py -o out/ma_cross_back.el
```

```
out/ma_cross_back.el: 3 builtins used | 3 PROVEN | 0 NOT-PROVEN | 0 ARITY-BAD | 0 RETURN-TYPE-BAD | 0 ARG-TYPE-BAD
OK -- every builtin used is known to compile in MultiCharts (names, argument counts, return types).
NOTE: argument types are checked only where the catalog declares a verified numeric parameter and the argument is a literal or a string/true-false variable; compiling in the MultiCharts editor is the final check.
```

`el_emit` exits non-zero if any builtin is not known to compile in
MultiCharts. To write a strategy by hand instead of transpiling one, see
[Writing a strategy in Python](PYTHON_STRATEGIES.md). The same works for a
larger strategy, from the library API:

```python
import os
from pl_transpiler import transpile
el = open("ground_truth/GT2_strategy_orders_position.txt").read()
os.makedirs("out", exist_ok=True)
open("out/GT2_mirror.py", "w").write(transpile(el, dialect="mirror"))
```

```bash
el_emit out/GT2_mirror.py -o out/GT2_from_python.txt
```

```
out/GT2_from_python.txt: 26 builtins used | 26 PROVEN | 0 NOT-PROVEN | 0 ARITY-BAD | 0 RETURN-TYPE-BAD | 0 ARG-TYPE-BAD
OK -- every builtin used is known to compile in MultiCharts (names, argument counts, return types).
NOTE: argument types are checked only where the catalog declares a verified numeric parameter and the argument is a literal or a string/true-false variable; compiling in the MultiCharts editor is the final check.
```

`el_emit` fails closed: every builtin must be in the shipped list of names
known to compile in MultiCharts, with the right argument count and return
type. Anything else is reported and the command exits non-zero. For example,
`Variance` is not on that list yet, so a mirror that calls it is reported as
NOT-PROVEN and `el_emit` exits 1. The GT1 mirror
(`ground_truth/GT1_functions_indicators.txt`, which uses `AvgTrueRange`,
`RateOfChange` and `StandardDev`) passes with 0 NOT-PROVEN.

Argument types are checked where both sides are certain: the catalog declares
the parameter `numeric` on a record verified against MultiCharts, and the
argument is a string literal, `true`/`false`, or a variable declared with a
string or true/false initial value. A mismatch is reported as ARG-TYPE-BAD
with the function, the argument position, the expected and found type, and
the line of the emitted EL, for example
`line 2: Power argument 2: expected numeric, found string`. Parameters typed
`any` or unknown, arguments that are expressions, and `documented-unverified`
records are skipped, never rejected. Compiling in the MultiCharts editor is
the final check.

## As a library

```python
from pl_transpiler import transpile, run_el, load_bars_csv, get_config

py = transpile(open("ground_truth/GT2_strategy_orders_position.txt").read())
out = run_el(
    open("ground_truth/GT2_strategy_orders_position.txt").read(),
    load_bars_csv("examples/NQ_sample_bars.csv"),
    get_config("NQ"),
    trace_columns=["MarketPosition", "NetProfit"],
)
print(len(py.splitlines()), "lines;", len(out["rows"]), "bars;", len(out["trades"]), "trades")
print(out["columns"], out["rows"][-1])
```

`transpile()` returns the runnable dialect (the same code `pl_transpile` writes
and `pl_run` executes), with EL Variables kept in a cross-bar `_state`. Pass
`dialect="mirror"` only for the clean Python that `el_emit` reads back; it
resets Variables every bar, so the bar loop refuses to run it.

Every public function, its options and return values, and each CLI's flags are
described in [docs/API.md](API.md).

```
65 lines; 140 bars; 7 trades
['MarketPosition', 'NetProfit'] ['1', '4742.20703125']
```

`out["trades"]` is the fill log: one entry per fill, so a reversal counts as
two (the exit and the new entry).

Look up a keyword:

```python
from pl_transpiler import catalog
print(catalog.get("DayFromDateTime"))
print(catalog.coverage())
```

```bash
python3 -m pl_transpiler.catalog --search date
```

For readable entries (what each supported keyword does, its arguments, how
well PowerBridge has verified it, and a runnable example), see the
[keyword reference](reference/README.md).

Agents and scripts: see [AGENTS.md](../AGENTS.md). LLM dialect notes:
[docs/LLM_DIALECT.md](LLM_DIALECT.md). Bar and capture format:
[docs/CAPTURES.md](CAPTURES.md).

## Your own data

`pl_run` and `pl_oracle` read a CSV of bars with a header row naming these
columns, in any order and any case: `date,time,open,high,low,close,volume`.
Extra columns are ignored. One row is one bar, oldest first.

| Column | Accepted forms |
| --- | --- |
| `date` | EL integer date, (year - 1900) × 10000 + month × 100 + day, so `1YYMMDD` for 2000-2099 (`1240102` is 2 Jan 2024), or three numbers separated by `/` or `-`: `D/M/YYYY` by default, `YYYY-MM-DD` (a 4-digit first field), or the order set by `--date-format` |
| `time` | EL integer `HHMM` (`930`, `1515`) or `HH:MM` / `HH:MM:SS` |
| `open` ... `volume` | numbers |

Rules:

- Slash dates are day-first by default, which is how the MultiCharts ASCII
  export writes them on a day-first Windows locale. `01/02/2024` is therefore
  1 February. For month-first data (US locale) pass `--date-format mdy`; for
  `YYYY/MM/DD` pass `--date-format ymd`.
- With the default, a date whose second field is above 12 (`01/13/2024`)
  stops the run with a hint to pass `--date-format mdy`. That catches most
  month-first files on the first day past the 12th, but a file that only holds
  days 1-12 cannot be told apart: check your export's date order.
- Months must be 1-12, days must exist in that month, and times must be
  00:00-23:59. Bar times have minute resolution, so a time with non-zero
  seconds (`09:31:05`) is an error rather than being rounded.
- Any bad value stops the run with one `error:` line naming the file, the CSV
  line and the value, and exit code 2.

For example, the bundled bars in MultiCharts export form give the same trade
digest as the EL-integer original:

```python
import csv
import os
os.makedirs("out", exist_ok=True)
rows = list(csv.reader(open("examples/NQ_sample_bars.csv")))
with open("out/my_bars.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Time", "Open", "High", "Low", "Close", "Volume"])
    for d, t, *ohlcv in rows[1:]:
        d, t = int(d), int(t)
        w.writerow(["%02d/%02d/%d" % (d % 100, d // 100 % 100, 1900 + d // 10000),
                    "%02d:%02d:00" % (t // 100, t % 100)] + ohlcv)
print(open("out/my_bars.csv").readlines()[1].strip())
```

```
02/01/2024,09:30:00,14997.50,15005.00,14992.50,15000.00,125
```

```bash
pl_oracle examples/ma_cross.el out/my_bars.csv
```

```
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

## Other instruments

`--config` takes a built-in name (`NQ`, the default, or `ES`) or a path to a
JSON file. The file's keys are exactly the fields of
`InstrumentConfig` in
[`pl_transpiler/runtime/instrument_config.py`](../pl_transpiler/runtime/instrument_config.py);
`category` is optional (default 0). The built-ins ship as
[`examples/configs/NQ.json`](../examples/configs/NQ.json) and
[`examples/configs/ES.json`](../examples/configs/ES.json): copy one and edit
`bigpointvalue` ($ per full point per contract), `pointvalue`, `minmove`,
`pricescale`, `tick`, the session start and the symbol fields. An unknown key,
a missing key or a wrong type stops the run with one `error:` line and exit 2.

The same bars and strategy with the ES point value ($50 instead of $20):

```bash
mkdir -p out
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv \
    --config examples/configs/ES.json --columns MarketPosition,NetProfit -o out/es_out.csv
```

```
[pl_run] 140 bars, 140 rows, 3 fills, 1 round-turns [python] -> out/es_out.csv
```

<!-- readme-test: file out/es_out.csv -->
```
MarketPosition,NetProfit
...
0,3359.50
...
```

The trade list (and so the `pl_oracle` digest) does not depend on the point
value; profit figures do.

## Warm-up and MaxBarsBack

PowerBridge evaluates every bar from bar 1, so `Average(Close, 21)` returns 0
for the first 20 bars; MultiCharts skips its MaxBarsBack bars (default 50)
instead. Pass `--max-bars-back 50` (or your chart's value) to `pl_oracle` /
`pl_run` to match. Without it, both print a `note:` on stderr when a strategy
reads past bars (or the `lookback past available history` warning instead,
when that applies); `-q` hides only the note. The note never changes the
output on stdout.

On the bars before MaxBarsBack history exists, a price lookback past the
start of the tape (`Close[5]` on bar 1) reads the current bar's Open, and
`Date[n]` / `Time[n]` read 0, for any `n`.

In the README quickstart, `--max-bars-back 21` is `ma_cross.el`'s own lookback
(a 21-bar average, compared with its previous value by `crosses`), so the demo
prints no note. The default is 0; pass your chart's value (the MultiCharts
default is 50) to match MultiCharts.

`--max-bars-back auto` (or `max_bars_back="auto"` in `run_oracle` / `run_el`)
uses the longest lookback PowerBridge finds in the strategy source, the same
number the warm-up note reports: `X[k]` counts k bars plus whatever `X` reads,
and a windowed builtin counts its length (a number or an input's default), so
`Highest(High, 10)[1]` gives 11 and `ma_cross.el` gives 21. The status line on
stderr shows the value it chose (`max-bars-back 21 (auto)`). When the scan
cannot bound the lookback (a non-constant index such as `Close[Len2]`, a length
that is an expression, `of N bars ago`, or a call to your own function), `auto`
stops with an error asking for a number instead of guessing: a silent 0 would
give the warm-up fills `auto` exists to avoid.

`auto` is not MultiCharts' auto-detect. MultiCharts finds MaxBarsBack at run
time, raising it and restarting the calculation when a study reads further back
than it has; `auto` is a static estimate made before the run, and it can differ
(it may count one bar more than the strict minimum, and it does not follow
lookbacks inside variables or recursive averages such as `XAverage`). To match
a MultiCharts chart exactly, pass the MaxBarsBack shown in its strategy
properties.

The default stays 0 rather than `auto` so that a run means exactly what its
flags say: every existing command, digest and test keeps its output, and a
strategy whose lookback cannot be inferred still runs.

## What the engine assumes

These hold whatever the config:

- **No commission and no slippage.** Profit is the fill-price difference times
  `bigpointvalue` times contracts, with nothing deducted; `next bar at market`
  fills at the next bar's open. The `Commission` and `Slippage` keywords
  return 0, and the native kernel is passed commission 0 and slippage 0.
- **No position limit.** `Buy` while short and `SellShort` while long
  reverse; `Sell` / `BuyToCover` only close. Repeat entries in the same
  direction always add (see [Limitations](LIMITATIONS.md)).
- `tick` is only used to round money-management stop and target fill prices
  (`SetStopLoss`, `SetProfitTarget`, trailing, breakeven) to the price grid.
- The session end is not configurable: it is 1515 (the @NQ#C / @ES#C close)
  for the session-end keywords and the native kernel.
