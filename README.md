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
The [keyword reference](docs/reference/README.md) lists the 261 builtins
PowerBridge covers: 254 run, and 7 transpile but stop at run time.

## Install

Python 3.9 or newer. In a fresh virtual environment (on Windows activate with
`.venv\Scripts\activate`), install the wheel attached to the
[v0.13.0 GitHub Release](https://github.com/hudsonwa/PowerBridge/releases/tag/v0.13.0):

<!-- readme-test: skip (creates a venv and installs over the network) -->
```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -U pip
pip install https://github.com/hudsonwa/PowerBridge/releases/download/v0.13.0/powerbridge-0.13.0-py3-none-any.whl
```

Or install the tagged release straight from the repository:

<!-- readme-test: skip (installs over the network) -->
```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@v0.13.0"
```

The optional native fast path is a separate wheel per platform,
`powerbridge_accel-0.13.0-cp39-abi3-<platform>.whl` on the same release page;
see [the platform table](docs/ACCEL.md#install). Releases are distributed
through GitHub Releases, not PyPI.

## Quickstart

The `powerbridge` distribution (import name `pl_transpiler`) installs five
commands: `pl_transpile`, `pl_run`, `pl_oracle`, `el_emit` and `pl_examples`.
Write the bundled example strategy and synthetic bars into `./examples/`
(it never overwrites; in a clone of this repository they are already there):

```bash
pl_examples
```

Backtest the example strategy on the synthetic bars and print its trade digest:

```bash
pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv
```

```
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

To see the run bar by bar (`pl_run` executes generated Python, so only run
files you trust):

```bash
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv --columns MarketPosition,NetProfit,TotalTrades
```

```
[pl_run] 140 bars, 119 rows, 3 fills, 1 round-turns, max-bars-back 21 (auto) [python]
MarketPosition,NetProfit,TotalTrades
0,0.00,0
...
0,1343.80,1
...
1,1343.80,1
```

The `[pl_run]` line is a status line on stderr. Which output goes where, the
`[python]` / `[native]` engine tag and the warm-up bars it skips are in
[the usage guide](docs/USAGE.md#walkthrough), which also covers
`--fills-csv` and [your own strategy and bars](docs/USAGE.md#your-own-strategy).

## Verified how

`pl_oracle` prints a SHA-256 digest of the exact trade list; digests on the
shipped sample bars are pinned in the tests and match on the native and Python
paths, and parity with MultiCharts is checked against licensed per-bar
captures that cannot ship here ([verification](docs/verification.md)).

## Documentation

| Doc | What it covers |
|---|---|
| [Usage guide](docs/USAGE.md) | The example step by step, your own strategy and bars, Python to EasyLanguage, other instruments |
| [Writing a strategy in Python](docs/PYTHON_STRATEGIES.md) | The mirror dialect from scratch |
| [Architecture](docs/ARCHITECTURE.md) | How it works: pipeline, bar loop, native kernel |
| [Design decisions](docs/DESIGN.md) | Why it is built this way |
| [Verification](docs/verification.md) | What you can check yourself; the trade digest |
| [Limitations](docs/LIMITATIONS.md) | Validation scope, unsupported keywords, known gaps |
| [API and CLI reference](docs/API.md) | Library API and every command's flags |
| [Keyword reference](docs/reference/README.md) | Every covered builtin |
| [Native fast path](docs/ACCEL.md) | Install, speed, fallback |
| [Contributing](CONTRIBUTING.md) | Running the tests, opening changes |
| [Changelog](CHANGELOG.md) | Release history |

Also: [bars and captures](docs/CAPTURES.md), [LLM dialect notes](docs/LLM_DIALECT.md),
[AGENTS.md](AGENTS.md) for agents and scripts.

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

See [CONTRIBUTING.md](CONTRIBUTING.md), which also covers running the tests and
why a run without the native kernel leaves out the tests that need it.
Open an issue before a large change. Small fixes can be a pull request on their own.

Development started in a private repository; this public repository begins at
the 0.2.0 export. Changes since then are made here in the open.
