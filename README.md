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

# PowerBridge

[![Public tests](https://github.com/hudsonwa/PowerBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/hudsonwa/PowerBridge/actions/workflows/ci.yml)
[![accel (Rust)](https://github.com/hudsonwa/PowerBridge/actions/workflows/accel.yml/badge.svg)](https://github.com/hudsonwa/PowerBridge/actions/workflows/accel.yml)

EasyLanguage / PowerLanguage to Python transpiler, and back.

**Status: preview.** Trade lists match MultiCharts fill-for-fill on the
1-minute @NQ ground-truth suite and on 1-minute @ES (four configurations of a
multi-entry strategy, 4,278 of 4,278 fills identical). An optional Rust kernel
runs the same backtest about 5x faster with identical fills
([measured](#acceleration) with `tools/bench_native.py`).

## See it work

`examples/ma_cross.el`:

```
Inputs: FastLen(9), SlowLen(21);
Vars: Fast(0), Slow(0);
Fast = Average(Close, FastLen);
Slow = Average(Close, SlowLen);
If Fast crosses above Slow Then Buy ("LE") Next Bar At Market;
If Fast crosses below Slow Then Sell ("LX") Next Bar At Market;
```

`pl_transpile examples/ma_cross.el` emits plain Python (excerpt):

<!-- readme-test: skip (excerpt of generated code) -->
```python
fast = f32(pl_average(close, fastlen))
slow = f32(pl_average(close, slowlen))
if pl_crosses_above((_state['fast'] + [fast]), (_state['slow'] + [slow])):
    _orders.append(('buy', 'market', 0, 'next', 'LE'))
```

`pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv` backtests it and
prints an exact trade digest (the same on the Rust and Python paths):

```
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

Write and test a strategy in Python, then emit EasyLanguage that passes name, argument-count and return-type checks for
MultiCharts or TradeStation. Or take existing EL and run it bar-by-bar in Python.
The import name is `pl_transpiler`. Everything is the Python standard library:
no PyPI deps, no network.

It would rather stop than guess. Unsupported keywords are reported in one pass.

## Install

Python 3.9 or newer, no other dependencies. Install the wheel attached to the
[v0.4.0 GitHub Release](https://github.com/hudsonwa/PowerBridge/releases/tag/v0.4.0):

<!-- readme-test: skip (installs over the network) -->
```bash
pip install https://github.com/hudsonwa/PowerBridge/releases/download/v0.4.0/powerbridge-0.4.0-py3-none-any.whl
```

The optional native fast path is a separate wheel per platform. Pick the
`powerbridge_accel-0.4.0-cp39-abi3-<platform>.whl` file for your machine from
the [release page](https://github.com/hudsonwa/PowerBridge/releases/tag/v0.4.0)
(see [the platform table](#acceleration)), for example:

<!-- readme-test: skip (installs over the network) -->
```bash
pip install https://github.com/hudsonwa/PowerBridge/releases/download/v0.4.0/powerbridge_accel-0.4.0-cp39-abi3-<platform>.whl
```

Or install the tagged release straight from the repository:

<!-- readme-test: skip (installs over the network) -->
```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@v0.4.0"
```

PyPI publishing is coming in a later release.

To work on PowerBridge itself, or to follow the walkthrough below with the
bundled examples, clone the repository and use the quickstart.

## Quickstart

Python 3.9 or newer, no other dependencies. Run these from the repository root.

**1. Install** (a virtual environment is recommended):

<!-- readme-test: skip (creates a venv and installs) -->
```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -U pip
pip install .
```

That installs the `powerbridge` distribution (import name `pl_transpiler`) and
four commands: `pl_transpile`, `pl_run`, `pl_oracle` and `el_emit`.

**2. Transpile an example** to Python:

```bash
pl_transpile examples/ma_cross.el -o ma_cross.py
```

```
[pl_transpile] wrote ma_cross.py
```

`ma_cross.py` is runnable: it defines `strategy(open, high, low, close, volume,
date, time, kwargs)`, takes the bar series so far, and keeps its cross-bar
state (`Vars`, lookback, `Inputs`) in `kwargs['_state']`. Pass the same
`kwargs` dict on every bar. To see its order signals, save this as `run.py`
next to `ma_cross.py` and run `python run.py` from the repository root:

```python
import csv
from ma_cross import strategy

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
    --config NQ --columns MarketPosition,NetProfit,TotalTrades -o run_out.csv
```

```
note: powerbridge-accel not installed; using the pure-Python engine (slower). See README#acceleration
[pl_run] 140 bars, 140 rows, 3 fills, 1 round-turns -> run_out.csv
```

The `note:` line goes to stderr and is expected without the optional
[Rust fast path](#acceleration); results are identical either way.

**4. Read the output.** `run_out.csv` has one row per bar with the columns you
asked for. The strategy goes long, exits with a
profit, and is long again at the end (excerpt):

<!-- readme-test: file run_out.csv -->
```
MarketPosition,NetProfit,TotalTrades
0,0.0,0
...
1,0.0,0
...
0,1343.7984375000087,1
...
1,1343.7984375000087,1
```

Column names are the run's trace keys; omit `--columns` to get all of them in
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

What you can verify yourself, digest gating and captures are in
[docs/verification.md](docs/verification.md).

## More usage

### Python (mirror dialect) to EasyLanguage

```python
from pl_transpiler import transpile
el = open("ground_truth/GT2_strategy_orders_position.txt").read()
open("GT2_mirror.py", "w").write(transpile(el, trace=False))
```

```bash
el_emit GT2_mirror.py -o GT2_from_python.txt
```

```
GT2_from_python.txt: 26 builtins used | 26 PROVEN | 0 NOT-PROVEN | 0 ARITY-BAD | 0 RETURN-TYPE-BAD
OK -- every builtin used is known to compile in MultiCharts (names, argument counts, return types).
```

`el_emit` fails closed: every builtin must be in the shipped list of names
known to compile in MultiCharts, with the right argument count and return
type. Anything else is reported and the command exits non-zero. For example,
`Variance` is not on that list yet, so a mirror that calls it is reported as
NOT-PROVEN and `el_emit` exits 1. The GT1 mirror
(`ground_truth/GT1_functions_indicators.txt`, which uses `AvgTrueRange`,
`RateOfChange` and `StandardDev`) passes with 0 NOT-PROVEN. Argument types are not checked; compiling in the MultiCharts editor
is the final check.

### As a library

```python
from pl_transpiler import transpile
from pl_transpiler.tools.pl_run import run_el, load_bars_csv
from pl_transpiler.runtime.instrument_config import get_config

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

```
108 lines; 140 bars; 7 trades
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

Agents and scripts: see [AGENTS.md](AGENTS.md). LLM dialect notes:
[docs/LLM_DIALECT.md](docs/LLM_DIALECT.md). Bar and capture format:
[docs/CAPTURES.md](docs/CAPTURES.md).

### Other instruments

`--config` takes a built-in name (`NQ`, the default, or `ES`) or a path to a
JSON file. The file's keys are exactly the fields of
`InstrumentConfig` in
[`pl_transpiler/runtime/instrument_config.py`](pl_transpiler/runtime/instrument_config.py);
`category` is optional (default 0). The built-ins ship as
[`examples/configs/NQ.json`](examples/configs/NQ.json) and
[`examples/configs/ES.json`](examples/configs/ES.json): copy one and edit
`bigpointvalue` ($ per full point per contract), `pointvalue`, `minmove`,
`pricescale`, `tick`, the session start and the symbol fields. An unknown key,
a missing key or a wrong type stops the run with one `error:` line and exit 2.

The same bars and strategy with the ES point value ($50 instead of $20):

```bash
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv \
    --config examples/configs/ES.json --columns MarketPosition,NetProfit -o es_out.csv
```

```
[pl_run] 140 bars, 140 rows, 3 fills, 1 round-turns -> es_out.csv
```

<!-- readme-test: file es_out.csv -->
```
MarketPosition,NetProfit
...
0,3359.496093750022
...
```

The trade list (and so the `pl_oracle` digest) does not depend on the point
value; profit figures do.

What the engine assumes, whatever the config:

- **No commission and no slippage.** Profit is the fill-price difference times
  `bigpointvalue` times contracts, with nothing deducted; `next bar at market`
  fills at the next bar's open. The `Commission` and `Slippage` keywords
  return 0, and the native kernel is passed commission 0 and slippage 0.
- **No position limit.** Every `Buy` / `SellShort` fills and adds to the
  position (there is no pyramiding cap); `Buy` while short and `SellShort`
  while long reverse; `Sell` / `BuyToCover` only close.
- `tick` is only used to round money-management stop and target fill prices
  (`SetStopLoss`, `SetProfitTarget`, trailing, breakeven) to the price grid.
- The session end is not configurable: it is 1515 (the @NQ#C / @ES#C close)
  for the session-end keywords and the native kernel.

## Acceleration

The optional `accel` fast path (Rust).

The core is **standard library only** in every install. The optional native
fast path is a Rust crate in `accel/` that builds the module
`pl_transpiler._accel`. It runs the whole bar loop and fill engine natively in
one FFI call (`pl_transpiler.runtime.accel.run_native`), plus batched input
sweeps (`run_native_sweep`) that compile and marshal the tape once.

**Prebuilt wheel (no Rust needed).** Each GitHub Release from v0.3.0 on
carries one `powerbridge-accel` wheel per platform, built for CPython 3.9 and
newer (abi3). The asset URL follows this pattern:

```
https://github.com/hudsonwa/PowerBridge/releases/download/vX.Y.Z/powerbridge_accel-X.Y.Z-cp39-abi3-PLATFORM.whl
```

Pick the file on the release page whose name ends with your platform:

| Platform | File name ends with |
|---|---|
| macOS Apple Silicon | `macosx_*_arm64.whl` |
| macOS Intel | `macosx_*_x86_64.whl` |
| Windows x64 | `win_amd64.whl` |
| Linux x86_64 | `manylinux*_x86_64.whl` |
| Linux aarch64 | `manylinux*_aarch64.whl` |

Then install core and the wheel, using the exact file URL:

<!-- readme-test: skip (template URL; installs over the network) -->
```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@vX.Y.Z"
pip install https://github.com/hudsonwa/PowerBridge/releases/download/vX.Y.Z/<file>.whl
```

**From source.** Build it from a checkout (needs a Rust toolchain):

<!-- readme-test: skip (needs a Rust toolchain) -->
```bash
pip install .                                    # core (pure stdlib)
pip install maturin
(cd accel && maturin build --release)            # accel wheel
pip install accel/target/wheels/powerbridge_accel-*.whl      # the native module
cd /tmp && python -c "import pl_transpiler._accel as a; print(a.ping())"  # 42
```

Working inside the repo instead? `./tools/build_accel.sh` builds the same
library and drops it next to the in-tree package, so tests run against it.

Indicative speed, not a promise. `python tools/bench_native.py --bars N`
times the GT2 sample strategy on a synthetic tape on both paths (median of 3
runs, whole `run_el` vs `run_native` call) and checks the fills are identical.
Measured on an Apple M4 laptop, CPython 3.14, release-lto build from
`./tools/build_accel.sh`:

| Bars | Fills | Python | Rust | Speedup |
|---:|---:|---:|---:|---:|
| 20,000 | 1,473 | 0.47 s | 0.087 s | 5.3x |
| 100,000 | 7,671 | 2.34 s | 0.42 s | 5.5x |

Both paths scale linearly with the bar count (the earlier super-linear
slowdown of the Python path, issue #4, is fixed). Measure your own strategy
before relying on a ratio.

**Absent extra = identical behaviour.** Without the module, the auditable
Python path runs and produces the same trades. Programs the native kernel
cannot execute faithfully raise `AccelRefused` (`AccelUnavailable` when the
module is missing) so the caller runs the Python path instead; they
never return a different result. CI builds and runs macOS arm64; the release
workflow builds and smoke-tests every platform in the table above. Windows 11 x64 was
checked by hand (rustup `x86_64-pc-windows-gnu` toolchain, no Visual Studio
needed); building from source on Linux is not CI-verified.

An installed module is found even when you run from inside the repository
folder. `./tools/build_accel.sh` builds it straight into the checkout instead.

## Unsupported keywords

Default is all or nothing. `pl_transpile` walks the whole file, lists every
unimplemented keyword with its line, writes no Python, and exits non-zero.

`--partial` is only for incremental porting. Stubs raise when they run. Do not
trust a backtest from partial output.

## Tests

<!-- readme-test: skip (the full suite; CI runs it as its own job) -->
```bash
python3 tests/run_public_tier.py      # Windows: py tests\run_public_tier.py
```

That is the portable suite. Capture-backed gates skip cleanly when the large
MultiCharts CSVs are absent. See [docs/CAPTURES.md](docs/CAPTURES.md) if you
want to attach your own.

## Limitations

- The Python to EL direction only reads the **mirror dialect**: the clean Python
  this transpiler itself emits. Arbitrary hand-written Python is out of scope.
- Validated against MultiCharts on **1-minute @NQ** and **1-minute @ES**
  (@ES#C RTH, 2016: four configurations of a real-world multi-entry strategy,
  4,278 of 4,278 fills identical). The MultiCharts captures behind these
  numbers are licensed and not in this repository; see
  [what you can verify](docs/verification.md#what-you-can-verify-from-this-repo).
  Other instruments and timeframes are untested.
- Some builtins are not implemented yet. A run that calls one **stops**
  with a one-line error naming the builtin and the first source line that
  uses it, rather than guess a value. To run anyway, pass `--lenient` to
  `pl_run` / `pl_oracle` or set `PL_STRICT=0`: those builtins then return
  0/'', the run ends with a `WARNING` naming them, and
  `run_el(...)["unimplemented"]` lists them. Cosmetic ones (drawing, sound,
  print) never stop a run and are not reported.
- Per-bar MultiCharts parity captures are not shipped. Those tests skip on a
  fresh clone.
- The `accel` fast path is optional and builds from source. It does not yet
  run strategies that declare **arrays**; those fall back to Python, loudly.
  `pl_run` (per-bar trace rows) always uses the Python path.
- `Sell from entry("A")` with two entries open closes the whole position.
  Per the documented EasyLanguage semantics it should close only entry A.
  This is documented behaviour that has not yet been verified against a
  MultiCharts capture (#47).
- Fail-loud assumes valid EasyLanguage. A residual gap: assigning to a
  read-only report keyword that is not in the bundled catalog can be treated as
  a local. Catalog-known names are still caught.
- This tree is the library, tests, and docs. Internal build tooling is not
  shipped.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

MultiCharts / TradeStation keyword PDFs and any `resources/` or `db/` reference
pack are third-party copyright and are **not** in this repository. The shipped
replacement is `pl_transpiler/tools/pl_signatures.jsonl` plus
`mc_verified_builtins.txt`.

PowerBridge is independent. It is not affiliated with TradeStation Technologies,
Inc. or MultiCharts (MCT Limited). Those names are used only to describe
compatibility.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue before a large change.
Small fixes can be a pull request on their own.

Development started in a private repository; this public repository begins at
the 0.2.0 export. Changes since then are made here in the open.
