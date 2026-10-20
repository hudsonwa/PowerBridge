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
runs the same backtest 13-97x faster with identical fills.

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

Python 3.9 or newer.

```bash
pip install .
```

That installs the `powerbridge` distribution (import name `pl_transpiler`) and four commands: `el_emit`,
`pl_run`, `pl_transpile`, and `pl_oracle`.

Examples below use `ground_truth/GT2_strategy_orders_position.txt` (a small
moving-average cross) and `examples/NQ_sample_bars.csv` (synthetic bars).
Run them from the repository root.

### The optional `accel` fast path (Rust)

The core is **standard library only** in every install. The optional native
fast path is a Rust crate in `accel/` that builds the module
`pl_transpiler._accel`. It runs the whole bar loop and fill engine natively in
one FFI call (`pl_transpiler.runtime.accel.run_native`), plus batched input
sweeps (`run_native_sweep`) that compile and marshal the tape once.

Build it from a checkout (needs a Rust toolchain):

```bash
pip install .                                    # core (pure stdlib)
pip install maturin
(cd accel && maturin build --release)            # accel wheel
pip install accel/target/wheels/powerbridge_accel-*.whl      # the native module
cd /tmp && python -c "import pl_transpiler._accel as a; print(a.ping())"  # 42
```

Working inside the repo instead? `./tools/build_accel.sh` builds the same
library and drops it next to the in-tree package, so tests run against it.

Indicative speed, not a promise: on an Apple M-series laptop, the GT2 sample
strategy over a 100,000-bar synthetic tape ran in 0.43 s native versus 42 s on
the Python path, with identical fills. Your strategy and tape will differ.
Reproduce with your own data before you rely on any ratio.

**Absent extra = identical behaviour.** Without the module, the auditable
Python path runs and produces the same trades. Programs the native kernel
cannot execute faithfully raise `AccelRefused` (`AccelUnavailable` when the
module is missing) so the caller runs the Python path instead; they
never return a different result. CI builds macOS arm64. Windows 11 x64 was
checked by hand (rustup `x86_64-pc-windows-gnu` toolchain, no Visual Studio
needed); Linux builds from source but is not CI-verified.

An installed module is found even when you run from inside the repository
folder. `./tools/build_accel.sh` builds it straight into the checkout instead.

## Quickstart

### EasyLanguage to Python, then a backtest

`pl_run` executes generated Python on your machine. Only run files you trust.
Transpiling a file does not execute it.

```bash
pl_run ground_truth/GT2_strategy_orders_position.txt examples/NQ_sample_bars.csv \
    --config NQ --columns marketpos,netprofit,totaltrades -o run_out.csv
```

Exact trade-list digest (the merge gate for performance work — two runs on
unchanged code must be byte-identical):

```bash
pl_oracle ground_truth/GT2_strategy_orders_position.txt examples/NQ_sample_bars.csv
```

`pl_oracle` uses the Rust kernel automatically when the `accel` module is
installed, and says so on stderr (`[native]`). `--python` forces the reference
Python path; the sha256 must match. Whenever a run ends up on the Python path,
a `WARNING: running the PYTHON path` line says why.

`pl_run` and `pl_oracle` accept an opt-in `--from-date` / `--from-bar` window.
Default is still the full tape. `pl_run` seeds the discarded prefix (state and
lookback stay primed, fills suppressed) unless you pass `--slice`. `pl_oracle
--from-date` stays a tape slice; pass `--seed-warmup` to seed instead. A
windowed run that disagrees on trades is a failed ticket, not a speed win.


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
    trace_columns=["marketpos", "netprofit"],
)
print(len(py.splitlines()), "lines;", len(out["rows"]), "bars;", len(out["trades"]), "trades")
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
  (@ES#C RTH, 2016: four configurations of a private multi-entry strategy,
  4,278 of 4,278 fills identical). Other instruments and timeframes are
  untested.
- Builtins the runtime does not implement yet return 0/'' and are reported:
  every `pl_run` / `pl_oracle` run prints a `WARNING` naming them, and
  `run_el(...)["unimplemented"]` lists them. `PL_STRICT=1` stops on the first
  call instead. Cosmetic ones (drawing, sound, print) are not reported.
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

This repository starts at a single commit. Earlier history is not included.
