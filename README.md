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

**Status: 0.2.0 preview.** Trade lists match MultiCharts fill-for-fill on the
1-minute @NQ ground-truth suite and on 1-minute @ES (four configurations of a
multi-entry strategy, 4,278 of 4,278 fills identical). An optional Rust kernel
runs the same backtest about 20x faster on 20,000 bars, and about 100x on
100,000 bars, with identical fills (`tools/bench_native.py`).

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

```python
fast = f32(pl_average(close, fastlen))
slow = f32(pl_average(close, slowlen))
if pl_crosses_above((_state['fast'] + [fast]), (_state['slow'] + [slow])):
    _orders.append(('buy', 'market', 0, 'next', 'LE'))
```

`pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv` backtests it and
prints an exact trade digest (the same on the Rust and Python paths):

```
powerbridge-trade-digest v1
fills: 3
round_turns: 1
sha256: 239484d32aa2945dabac2f68d739786fea5388cdb150bc54e4d799bef344d06e
```

Write and test a strategy in Python, then emit EasyLanguage that passes name, argument-count and return-type checks for
MultiCharts or TradeStation. Or take existing EL and run it bar-by-bar in Python.
The import name is `pl_transpiler`. Everything is the Python standard library:
no PyPI deps, no network.

It would rather stop than guess. Unsupported keywords are reported in one pass.

## Install

Python 3.9 or newer, no other dependencies. Install a tagged release straight
from the repository:

```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@v0.2.0"
```

Once the first release is published to PyPI, `pip install powerbridge` (and
`pip install powerbridge-accel` for the native fast path) will work as well.
Until then, use the GitHub install above.

To work on PowerBridge itself, or to follow the walkthrough below with the
bundled examples, clone the repository and use the quickstart.

## Quickstart

Python 3.9 or newer, no other dependencies. Run these from the repository root.

**1. Install** (a virtual environment is recommended):

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
powerbridge-trade-digest v1
fills: 3
round_turns: 1
sha256: 239484d32aa2945dabac2f68d739786fea5388cdb150bc54e4d799bef344d06e
```

What you can verify yourself, digest gating and captures are in
[docs/verification.md](docs/verification.md).

## More usage

### Python (mirror dialect) to EasyLanguage

```python
from pl_transpiler import transpile
el = open("ground_truth/GT1_functions_indicators.txt").read()
open("GT1_mirror.py", "w").write(transpile(el, trace=False))
```

```bash
el_emit GT1_mirror.py -o GT1_from_python.txt
```

`el_emit` fails closed: if the emitted EL is not compile-safe it writes nothing
usable and exits non-zero. Without the optional MultiCharts keyword index (not
shipped; see Limitations) the name-proof step is skipped and only argument-count
and return-type checks run.

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
108 lines; 140 bars; 4 trades
['MarketPosition', 'NetProfit'] ['1', '4742.20703125']
```

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

## Acceleration

The optional `accel` fast path (Rust).

The core is **standard library only** in every install. The optional native
fast path is a Rust crate in `accel/` that builds the module
`pl_transpiler._accel`. It runs the whole bar loop and fill engine natively in
one FFI call (`pl_transpiler.runtime.accel.run_native`), plus batched input
sweeps (`run_native_sweep`) that compile and marshal the tape once.

**Prebuilt wheel (no Rust needed).** Each GitHub Release from v0.2.1 on
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

```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@vX.Y.Z"
pip install https://github.com/hudsonwa/PowerBridge/releases/download/vX.Y.Z/<file>.whl
```

**From source.** Build it from a checkout (needs a Rust toolchain):

```bash
pip install .                                    # core (pure stdlib)
pip install maturin
(cd accel && maturin build --release)            # accel wheel
pip install accel/target/wheels/powerbridge_accel-*.whl      # the native module
cd /tmp && python -c "import pl_transpiler._accel as a; print(a.ping())"  # 42
```

Working inside the repo instead? `./tools/build_accel.sh` builds the same
library and drops it next to the in-tree package, so tests run against it.

Indicative speed, not a promise. `python tools/bench_native.py --bars 20000`
times the GT2 sample strategy on a synthetic tape on both paths and checks the
fills are identical. On an Apple M-series laptop: about 20x at 20,000 bars and
about 100x at 100,000 bars (the Python path slows down faster than the bar
count grows; see issue #4). Measure your own strategy before relying on a ratio.

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
