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

The digest goes to stdout. A status line goes to stderr, so it shows in your
terminal but not in a redirected file:
`[pl_oracle] 140 bars, 3 fills, 1 round-turns [python]` (`[native]` when the
Rust kernel ran). The digest is the same either way. To see the run bar by
bar (`pl_run` executes generated Python, so only run files you trust):

```bash
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv --max-bars-back 21 --columns MarketPosition,NetProfit,TotalTrades
```

```
[pl_run] 140 bars, 119 rows, 3 fills, 1 round-turns [python]
MarketPosition,NetProfit,TotalTrades
0,0.00,0
...
0,1343.80,1
...
1,1343.80,1
```

The first line is the status line on stderr; it ends `[native]` instead
when the Rust kernel wrote the rows (the CSV is byte-identical either way).

`--max-bars-back 21` is the strategy's own lookback; why it matters is in
[Warm-up and MaxBarsBack](docs/USAGE.md#warm-up-and-maxbarsback).

## Your own strategy

Put your EasyLanguage in a `.el` (or `.txt`) file and your bars in a CSV with
this header, in any order and any case (extra columns are ignored), one bar per
row, oldest first:

<!-- readme-test: file examples/NQ_sample_bars.csv -->
```
date,time,open,high,low,close,volume
1240102,930,14997.50,15005.00,14992.50,15000.00,125
...
```

`date` is an EL integer (`1240102` is 2 Jan 2024) or `D/M/YYYY` / `YYYY-MM-DD`
(`--date-format mdy` for US month-first dates); `time` is `HHMM` or `HH:MM`.
The [full rules](docs/USAGE.md#your-own-data) cover the edge cases.

For example, save this as `out/breakout.el` (run `mkdir -p out` first):

<!-- readme-test: write out/breakout.el -->
```
Inputs: Len(10);
If MarketPosition = 0 and Close > Highest(High, Len)[1] Then
    Buy ("BO") Next Bar At Market;
If BarsSinceEntry >= 5 Then Sell ("BX") Next Bar At Market;
```

It buys a 10-bar breakout when flat and sells five bars later:

```bash
pl_oracle out/breakout.el examples/NQ_sample_bars.csv --max-bars-back 11
```

```
powerbridge-trade-digest v2
fills: 6
round_turns: 3
sha256: 856a5cb26942203c8f757fa91e620b1eaebbdef0b511ab42952563e23c57225c
```

```bash
pl_run out/breakout.el examples/NQ_sample_bars.csv --max-bars-back 11 -o out/breakout_trace.csv
```

```
[pl_run] 140 bars, 129 rows, 6 fills, 3 round-turns [python] -> out/breakout_trace.csv
```

A round turn is an exit fill, so a strategy that only adds entries reports
`0 round-turns`. Without the `MarketPosition = 0` check, every new high would
add another contract: same-direction entries pyramid by default
(`--max-entries-per-direction 1` caps them).

Set `--max-bars-back` to the longest lookback your strategy reads (here
`Highest(High, 10)[1]` reads 11 bars), and `--config ES` or
`--config my_instrument.json` for an instrument other than NQ
([other instruments](docs/USAGE.md#other-instruments)).

If the file uses a keyword PowerBridge does not implement, nothing runs: the
command prints `error: unimplemented EL keyword '...' at line N` and exits
with code 2. Check [limitations](docs/LIMITATIONS.md#unsupported-keywords) and
the [keyword reference](docs/reference/README.md), rewrite that line with
supported keywords, or open an issue naming the keyword.

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
