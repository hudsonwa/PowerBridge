# PowerBridge

[![Public tests](https://github.com/hudsonwa/PowerBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/hudsonwa/PowerBridge/actions/workflows/ci.yml)
[![accel (Rust)](https://github.com/hudsonwa/PowerBridge/actions/workflows/accel.yml/badge.svg)](https://github.com/hudsonwa/PowerBridge/actions/workflows/accel.yml)

PowerBridge transpiles EasyLanguage / PowerLanguage strategies to Python and
backtests them bar by bar, with trade lists that match MultiCharts
fill-for-fill on its ground-truth suites. It also goes back: a strategy written
in PowerBridge's small Python "mirror dialect" (not general Python) is emitted
as checked EasyLanguage. Pure Python standard library, no PyPI dependencies, no
network; an optional Rust kernel runs the same backtests about 5x faster.
**Status: beta.** It would rather stop than guess: unsupported keywords are
reported, not approximated ([status and scope](docs/USAGE.md#status-and-scope),
[limitations](docs/LIMITATIONS.md)).

## Install

Python 3.9 or newer. Install the wheel attached to the
[v0.8.0 GitHub Release](https://github.com/hudsonwa/PowerBridge/releases/tag/v0.8.0):

<!-- readme-test: skip (installs over the network) -->
```bash
pip install https://github.com/hudsonwa/PowerBridge/releases/download/v0.8.0/powerbridge-0.8.0-py3-none-any.whl
```

Or install the tagged release straight from the repository:

<!-- readme-test: skip (installs over the network) -->
```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@v0.8.0"
```

The optional native fast path is a separate wheel per platform,
`powerbridge_accel-0.8.0-cp39-abi3-<platform>.whl` on the same release page;
see [the platform table](docs/ACCEL.md#install). PyPI publishing is coming in a
later release.

## Quickstart

From a fresh clone (on Windows activate with `.venv\Scripts\activate`):

<!-- readme-test: skip (clones, creates a venv and installs) -->
```bash
git clone https://github.com/hudsonwa/PowerBridge && cd PowerBridge
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -U pip && pip install .
```

That installs the `powerbridge` distribution (import name `pl_transpiler`) and
four commands: `pl_transpile`, `pl_run`, `pl_oracle` and `el_emit`. Backtest
the bundled example strategy on the bundled synthetic bars and print its trade
digest:

```bash
pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv --max-bars-back 21
```

```
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

The digest is the same with or without the Rust kernel. To see the run bar by
bar (`pl_run` executes generated Python, so only run files you trust):

```bash
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv --max-bars-back 21 --columns MarketPosition,NetProfit,TotalTrades
```

```
[pl_run] 140 bars, 119 rows, 3 fills, 1 round-turns
MarketPosition,NetProfit,TotalTrades
0,0.00,0
...
0,1343.80,1
...
1,1343.80,1
```

`--max-bars-back 21` is the strategy's own lookback; why it matters is in
[Warm-up and MaxBarsBack](docs/USAGE.md#warm-up-and-maxbarsback).

## Next steps

- [Usage guide](docs/USAGE.md): the example step by step, the
  [round trip back to EasyLanguage](docs/USAGE.md#python-mirror-dialect-to-easylanguage),
  the library API, your own bar data, other instruments and what the engine
  assumes.
- [Writing a strategy in Python](docs/PYTHON_STRATEGIES.md): the mirror
  dialect from scratch, with a worked example in `examples/hand_written.py`.
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
- [CHANGELOG](CHANGELOG.md).

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
