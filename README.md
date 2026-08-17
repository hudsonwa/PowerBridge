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

EasyLanguage / PowerLanguage to Python transpiler, and back.

Write and test a strategy in Python, then emit compile-safe EasyLanguage for
MultiCharts or TradeStation. Or take existing EL and run it bar-by-bar in Python.
The import name is `pl_transpiler`. Everything is the Python standard library:
no PyPI deps, no network.

It would rather stop than guess. Unsupported keywords are reported in one pass.

## Install

Python 3.9 or newer.

```bash
pip install .
```

That installs the `pl_transpiler` package and three commands: `el_emit`,
`pl_run`, and `pl_transpile`.

Examples below use `ground_truth/GT2_strategy_orders_position.txt` (a small
moving-average cross) and `examples/NQ_sample_bars.csv` (synthetic bars).
Run them from the repository root.

## Quickstart

### EasyLanguage to Python, then a backtest

`pl_run` executes generated Python on your machine. Only run files you trust.
Transpiling a file does not execute it.

```bash
pl_run ground_truth/GT2_strategy_orders_position.txt examples/NQ_sample_bars.csv \
    --config NQ --columns marketpos,netprofit,totaltrades -o run_out.csv
```

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
python3 tests/run_public_tier.py
```

That is the portable suite. Capture-backed gates skip cleanly when the large
MultiCharts CSVs are absent. See [docs/CAPTURES.md](docs/CAPTURES.md) if you
want to attach your own.

## Limitations

- The Python to EL direction only reads the **mirror dialect**: the clean Python
  this transpiler itself emits. Arbitrary hand-written Python is out of scope.
- Checked against **1-minute @NQ and @ES** bars only. Other instruments and
  timeframes are untested.
- Per-bar MultiCharts parity captures are not shipped. Those tests skip on a
  fresh clone.
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
