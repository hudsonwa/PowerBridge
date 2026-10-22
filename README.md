# PowerBridge

[![Public tests](https://github.com/hudsonwa/PowerBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/hudsonwa/PowerBridge/actions/workflows/ci.yml)
[![accel (Rust)](https://github.com/hudsonwa/PowerBridge/actions/workflows/accel.yml/badge.svg)](https://github.com/hudsonwa/PowerBridge/actions/workflows/accel.yml)

EasyLanguage / PowerLanguage to Python transpiler, and back.

**Status: beta.** Trade lists match MultiCharts fill-for-fill on the
1-minute @NQ ground-truth suite and on 1-minute @ES (four configurations of a
multi-entry strategy, 4,278 of 4,278 fills identical). An optional Rust kernel
runs the same backtest about 5x faster with identical fills
([measured](docs/ACCEL.md#speed) with `tools/bench_native.py`).

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

Take existing EL and run it bar-by-bar in Python. Or write a strategy in
PowerBridge's small Python "mirror dialect" (not general Python) and emit
EasyLanguage that passes name, argument-count and return-type checks for
MultiCharts or TradeStation, then backtest that EL here.
The import name is `pl_transpiler`. Everything is the Python standard library:
no PyPI deps, no network.

It would rather stop than guess. Unsupported keywords are reported in one pass
(see [Limitations](docs/LIMITATIONS.md)).

## Install

Python 3.9 or newer, no other dependencies. Install the wheel attached to the
[v0.7.0 GitHub Release](https://github.com/hudsonwa/PowerBridge/releases/tag/v0.7.0):

<!-- readme-test: skip (installs over the network) -->
```bash
pip install https://github.com/hudsonwa/PowerBridge/releases/download/v0.7.0/powerbridge-0.7.0-py3-none-any.whl
```

The optional native fast path is a separate wheel per platform. Pick the
`powerbridge_accel-0.7.0-cp39-abi3-<platform>.whl` file for your machine from
the [release page](https://github.com/hudsonwa/PowerBridge/releases/tag/v0.7.0)
(see [the platform table](docs/ACCEL.md#install)), for example:

<!-- readme-test: skip (installs over the network) -->
```bash
pip install https://github.com/hudsonwa/PowerBridge/releases/download/v0.7.0/powerbridge_accel-0.7.0-cp39-abi3-<platform>.whl
```

Or install the tagged release straight from the repository:

<!-- readme-test: skip (installs over the network) -->
```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@v0.7.0"
```

PyPI publishing is coming in a later release.

## Quickstart

From a fresh clone, in a virtual environment (Python 3.9 or newer; on Windows
activate with `.venv\Scripts\activate`):

<!-- readme-test: skip (clones, creates a venv and installs) -->
```bash
git clone https://github.com/hudsonwa/PowerBridge && cd PowerBridge
python3 -m venv .venv && . .venv/bin/activate && pip install .
```

That installs the `powerbridge` distribution (import name `pl_transpiler`) and
four commands: `pl_transpile`, `pl_run`, `pl_oracle` and `el_emit`. Then:

```bash
pl_transpile examples/ma_cross.el -o ma_cross.py
```

```
[pl_transpile] wrote ma_cross.py
```

```bash
pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv
```

```
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

```bash
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv --columns MarketPosition,NetProfit,TotalTrades
```

```
note: pl_run: strategy reads past bars and --max-bars-back is 0; fills may differ from MultiCharts during warm-up (set --max-bars-back N, or -q to hide this)
[pl_run] 140 bars, 140 rows, 3 fills, 1 round-turns
MarketPosition,NetProfit,TotalTrades
0,0.00,0
...
0,1343.80,1
...
1,1343.80,1
```

And back: transpile to the mirror dialect, then emit checked EasyLanguage:

```bash
pl_transpile examples/ma_cross.el --no-persist -o ma_mirror.py
```

```
[pl_transpile] wrote ma_mirror.py
```

```bash
el_emit ma_mirror.py -o ma_cross_back.el
```

```
ma_cross_back.el: 3 builtins used | 3 PROVEN | 0 NOT-PROVEN | 0 ARITY-BAD | 0 RETURN-TYPE-BAD | 0 ARG-TYPE-BAD
OK -- every builtin used is known to compile in MultiCharts (names, argument counts, return types).
NOTE: argument types are checked only where the catalog declares a verified numeric parameter and the argument is a literal or a string/true-false variable; compiling in the MultiCharts editor is the final check.
```

`pl_transpile` writes plain Python; `pl_oracle` backtests the strategy on the
bundled synthetic bars and prints a trade digest (its `[pl_oracle] 140 bars, …`
summary line goes to stderr); `pl_run` prints one CSV row
per bar (add `-o file.csv` to write a file). Without the optional
[Rust fast path](#acceleration) the pure-Python engine runs (add `-v` to get a
`note:` saying so); results are identical either way. `pl_run` executes the generated Python, so only run files you trust.
`el_emit` exits non-zero if any builtin is not known to compile in MultiCharts.
To write a strategy by hand instead of transpiling one, see
[Writing a strategy in Python](docs/PYTHON_STRATEGIES.md): the dialect from
scratch, with a worked example in `examples/hand_written.py`.

**Warm-up differs from MultiCharts by default.** PowerBridge evaluates every
bar from bar 1, so `Average(Close, 21)` returns 0 for the first 20 bars;
MultiCharts skips its MaxBarsBack bars (default 50) instead. Pass
`--max-bars-back 50` (or your chart's value) to `pl_oracle` / `pl_run` to
match. Without it, both print the `note:` above on stderr when a strategy reads
past bars; `-q` hides it. The note never changes the output on stdout.
The [usage guide](docs/USAGE.md) walks through each step in detail.

## Acceleration

The optional Rust kernel (`powerbridge-accel`) runs the same backtests about 5x
faster with identical fills. Install paths (release wheel or from source), the
platform table and benchmarks are in [docs/ACCEL.md](docs/ACCEL.md#install).

## Learn more

- [Usage guide](docs/USAGE.md): the full walkthrough, Python to EasyLanguage,
  the library API, your own bar data (CSV and date formats), other instruments
  and what the engine assumes.
- [Limitations](docs/LIMITATIONS.md): validation scope, unsupported keywords and
  known gaps.
- [Native fast path](docs/ACCEL.md): install, speed and how the fallback works.
- [API and CLI reference](docs/API.md) and the
  [keyword reference](docs/reference/README.md).
- [Verification](docs/verification.md): what you can check yourself, and the
  trade digest.
- [Bars and captures](docs/CAPTURES.md), [LLM dialect notes](docs/LLM_DIALECT.md),
  [AGENTS.md](AGENTS.md) for agents and scripts.
- [Architecture](docs/ARCHITECTURE.md) and [design decisions](docs/DESIGN.md).

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

See [CONTRIBUTING.md](CONTRIBUTING.md), which also covers running the tests.
Open an issue before a large change. Small fixes can be a pull request on their own.

Development started in a private repository; this public repository begins at
the 0.2.0 export. Changes since then are made here in the open.
