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

> **Convert Python trading strategies to EasyLanguage / PowerLanguage — and back.**
> A bidirectional EasyLanguage/PowerLanguage ⇄ Python transpiler for MultiCharts and
> TradeStation.

## TLDR

PowerBridge lets you develop trading strategies in Python (including with LLMs), then
reliably convert strategies written in its supported Python dialect (see Limitations) into
compile-safe EasyLanguage/PowerLanguage for live execution on MultiCharts and TradeStation —
while keeping your proprietary logic private.

- **Turn LLM-generated Python — written in the supported mirror dialect (see Limitations) —
  into compile-safe EasyLanguage/PowerLanguage** you can run on MultiCharts or TradeStation.
- **Keep sensitive strategy code out of third-party AI services** — research locally and never
  paste proprietary logic into a hosted LLM.
- **Combine Python's superior backtesting and research capabilities with
  MultiCharts/TradeStation's live execution strengths** — the right tool for each job.
- **Bridge modern Python workflows with legacy but trusted trading platforms** you already run
  live.

Everything is **pure Python standard library** — the transpiler, emitter, runtime, and gates
import nothing off PyPI, and nothing touches the network.

## What PowerBridge does

- **Turn LLM-generated Python into live trading code** — Use modern LLMs to rapidly develop
  and test strategy ideas in Python, then convert them into compile-safe
  EasyLanguage/PowerLanguage — within the supported mirror dialect (see Limitations) — that you
  can run live on MultiCharts or TradeStation.
- **Best of both worlds** — Leverage Python's strengths in research, backtesting, and rapid
  iteration, while using MultiCharts/TradeStation for reliable, low-latency live execution.
- **Bridge legacy platforms with modern workflows** — Keep using the trading platforms you
  trust for live trading, while gaining access to Python's ecosystem for strategy development
  and analysis.

## Features & benefits

- Convert LLM-generated Python strategies — written in the supported mirror dialect (see
  Limitations) — into compile-safe EasyLanguage/PowerLanguage
- Keep sensitive strategy code out of third-party AI services
- Combine Python's superior backtesting and research capabilities with
  MultiCharts/TradeStation's live execution strengths
- Bridge modern Python workflows with legacy but trusted trading platforms

## Privacy & control

**Keep your trading strategy private from ChatGPT and Claude.** Everything runs locally, so
your logic never has to leave your machine.

- **Keep your strategy logic private** — Develop and refine ideas in Python without pasting
  sensitive strategy code into Claude, GPT, or other LLM services.
- **Protect proprietary intellectual property** — Maintain full control over your trading logic
  instead of sending it to third-party AI providers.

## Why Python for research, MultiCharts/TradeStation for execution

- **LLMs are great at Python, not PowerLanguage** — Most large language models produce
  high-quality Python but struggle significantly with EasyLanguage/PowerLanguage. PowerBridge
  lets you use the best tool for idea generation, then handles the translation within its
  supported dialect (see Limitations).
- **Python for research. MultiCharts/TradeStation for execution.** — Python excels at
  backtesting, data analysis, and machine learning. MultiCharts and TradeStation excel at live
  order execution and platform stability. PowerBridge connects the two.
- **Useful for traders who haven't migrated** — While many have moved to QuantConnect,
  TradingView, or other platforms, a significant number of serious traders still rely on
  MultiCharts and TradeStation for live trading. PowerBridge makes modern development practices
  viable on these established platforms.

## PowerBridge vs migrating to another platform

While many traders have moved to QuantConnect, TradingView, or other platforms, a significant
number of serious traders still rely on MultiCharts and TradeStation for live trading.
PowerBridge is built for them: it brings a modern Python (and LLM-assisted) development workflow
to the platforms they already run, rather than asking them to migrate.

| | Migrating to another platform | PowerBridge |
|---|---|---|
| Live execution | Rebuild on the new platform | Keep your existing MultiCharts/TradeStation execution |
| Research in Python | Depends on the platform | Prototype and backtest locally in Python |
| LLM-assisted drafting | Depends on the platform | Draft in Python (mirror dialect), convert to EL |
| Strategy logic | Often hosted on the vendor's servers | Stays local on your machine |

These other platforms are migration destinations, not rivals PowerBridge measures itself
against — see [Limitations](#limitations) for exactly what has and hasn't been validated.

## PowerBridge vs other EasyLanguage/Python tools

Compared with one-way EasyLanguage-to-Python converters, PowerBridge is:

- **Bidirectional and round-trip aware** — it converts EasyLanguage → Python and Python → EL,
  and round-trips EL → AST → EL through one shared representation.
- **Backed by a verified keyword catalog** — a distilled, machine-readable EasyLanguage /
  PowerLanguage keyword catalog drives the argument-count and return-type checks.
- **Fail-loud by design** — unsupported constructs raise rather than emitting
  plausible-but-wrong output (see [Limitations](#limitations) for the residual gaps).
- **Runnable** — a bar-by-bar Python runtime and fill engine lets a converted strategy actually
  run over your own OHLCV bars.

See [Limitations](#limitations) for exactly what has and hasn't been validated.

## FAQ

### Can PowerBridge convert Python to EasyLanguage? (a MultiCharts Python converter)

Yes — that is the Python → EasyLanguage direction. It converts Python written in PowerBridge's
supported mirror dialect (the clean Python the transpiler itself emits — see Limitations) into
canonical, compile-safe EasyLanguage/PowerLanguage. Arbitrary hand-written Python is out of
scope and raises rather than guessing.

### Can it convert EasyLanguage to Python?

Yes. The EasyLanguage → Python direction transpiles EL/PowerLanguage source into Python and can
run it bar-by-bar over your own OHLCV data. Round-trip fidelity is semantic, not textual —
comments, whitespace, and casing are normalised, not preserved byte-for-byte.

### Can I develop a TradeStation strategy in Python?

You can prototype and backtest the logic in Python, then convert it to EasyLanguage for
TradeStation — provided the Python stays within the supported mirror dialect (see Limitations).
Validation to date is against 1-minute @NQ and @ES intraday index futures only; other
instruments and timeframes are untested.

### Can I use ChatGPT or Claude to write MultiCharts strategies? (LLM trading strategies for MultiCharts)

Indirectly, and that is the intended workflow: LLMs write good Python but struggle with
EasyLanguage/PowerLanguage. Have the LLM draft the logic in the supported mirror dialect (the
[LLM dialect cheatsheet](docs/LLM_DIALECT.md) gives a copy-pasteable system prompt), then let
PowerBridge convert it to EL. Output outside the dialect is rejected, not guessed at.

### How does it keep my trading strategy private from ChatGPT and Claude?

You develop and convert everything locally. PowerBridge is pure Python standard library with no
network access, so your strategy logic never leaves your machine — you never have to paste
proprietary code into a hosted LLM service. If you do choose to use an LLM for drafting, you
share only as much as you decide to.

### Can I backtest an EasyLanguage strategy in Python?

Yes — `pl_run` transpiles an EL strategy to Python and runs it bar-by-bar over OHLCV bars you
supply, returning per-bar trace columns and the orders/position/trades list. Note the trust
model: `pl_run` executes generated Python on your machine, so only run strategy files you trust;
converting or transpiling a file alone never executes it.

### Is PowerBridge affiliated with MultiCharts or TradeStation?

No. PowerBridge is an independent open-source project with no affiliation with, and no
endorsement or sponsorship from, TradeStation or MultiCharts; the vendor names are used only to
describe compatibility. The full trademark notice is in the [Licensing](#licensing) section
below.

### What are the main limitations?

The Python side is a bounded mirror dialect (not arbitrary Python); parity is validated only
against 1-minute @NQ and @ES intraday index futures; the headline MultiCharts parity captures
are private and the capture-dependent tests skip on a fresh clone; and `pl_run` executes
generated Python with no isolation. These are deliberate scoping choices — see the full
[Limitations](#limitations) section below.

## Install

```bash
pip install .
```

This installs the `pl_transpiler` package and two console entrypoints, `el_emit`
and `pl_run`. Python 3.9+ is required. Nothing off PyPI is pulled in.

The examples below use a strategy that ships with the project,
`ground_truth/GT2_strategy_orders_position.txt` (a small moving-average-cross
signal), and a tiny synthetic bar file, `examples/NQ_sample_bars.csv`. Run them
from the repository root.

## Quickstart

### Workflow A — Python → EL → MultiCharts (`el_emit`)

`el_emit` turns a `.py` file (clean mirror-dialect Python → EL) or any EL file
(EL → AST → canonical EL round-trip) into canonical, compile-safe EL. It
**fail-closes**: if the emitted EL is not compile-safe it writes nothing usable and
exits non-zero. On every run it prints its compile-safety audit table (the
`mc_ground_check` report) to stdout, so that verbose output is expected — a zero exit
means the emitted EL passed every check.

Round-trip a shipping EL indicator through the shared AST back to canonical EL:

```bash
el_emit ground_truth/GT1_functions_indicators.txt -o GT1_emitted.txt
```

Now the full Python → EL direction. First get the clean "mirror dialect" Python for
that indicator (this is exactly the Python `el_emit` knows how to read back):

```python
from pl_transpiler import transpile
el = open("ground_truth/GT1_functions_indicators.txt").read()
open("GT1_mirror.py", "w").write(transpile(el, trace=False))
```

Then turn that Python back into canonical EL you can paste into MultiCharts:

```bash
el_emit GT1_mirror.py -o GT1_from_python.txt
```

`el_emit`'s fail-closed check is strongest when the MultiCharts keyword reference is
present on disk (that copyrighted corpus is **not** included in this distribution).
Without it the name-proof step is skipped and only argument-count and
return-type checks run (see [docs/CAPTURES.md](docs/CAPTURES.md) and the Limitations
below).

### Workflow B — EL → Python → backtest (`pl_run`)

`pl_run` transpiles EL to Python and runs it bar-by-bar over supplied OHLCV bars and
an instrument config, returning the per-bar trace columns plus the
orders/position/trades list — no ground-truth captures required. It is an **installed
console script**, so after `pip install .` it runs from **any directory** with no
source checkout. **Trust model:** `pl_run` **executes generated Python** on your
machine — only run strategy files you trust; converting or transpiling a file alone
never executes it.

```python
# pl_run works from any directory on any OS: copy the sample strategy + bars into a
# scratch dir and run the installed console script there.
import os, shutil, subprocess, tempfile

demo = tempfile.mkdtemp(prefix="pl_run_demo_")
for f in ("ground_truth/GT2_strategy_orders_position.txt",
          "examples/NQ_sample_bars.csv"):
    shutil.copy(f, os.path.join(demo, os.path.basename(f)))
subprocess.run([
    "pl_run", "GT2_strategy_orders_position.txt", "NQ_sample_bars.csv",
    "--config", "NQ", "--columns", "marketpos,netprofit,totaltrades",
    "-o", "run_out.csv",
], cwd=demo, check=True)
print("demo dir:", demo)
print("output:", os.path.join(demo, "run_out.csv"))
```

`examples/NQ_sample_bars.csv` is a plain OHLCV file with the header
`date,time,open,high,low,close,volume` (`date` as EL `YYYMMDD` — e.g. `1240102`
for 2024-01-02 — and `time` as `HHmm`). It is **synthetic** sample data, not a real
market feed
— bring your own bars for real work. See [docs/CAPTURES.md](docs/CAPTURES.md) for the
data format in detail.

### Use it as a library

```python
from pl_transpiler import transpile              # EL -> Python
py_code = transpile(open("ground_truth/GT2_strategy_orders_position.txt").read())
print("generated", len(py_code.splitlines()), "lines of Python")

from pl_transpiler.tools.pl_run import run_el, load_bars_csv
from pl_transpiler.runtime.instrument_config import get_config

out = run_el(open("ground_truth/GT2_strategy_orders_position.txt").read(),
             load_bars_csv("examples/NQ_sample_bars.csv"),
             get_config("NQ"),
             trace_columns=["marketpos", "netprofit"])
# out = {"columns": [...], "rows": [[...], ...], "trades": [...]}
print(out["columns"], len(out["rows"]), "rows,", len(out["trades"]), "trades")
```

## Reporting every unsupported keyword at once

When the transpiler meets an EasyLanguage keyword it does not implement, it does
not stop at the first one. It reads the whole strategy, collects **every**
unsupported keyword together with the line it sits on, prints one complete report,
and writes no Python. You fix them as a batch instead of one recompile at a time.

`pl_transpile` is the forward transpiler on the command line — it installs
alongside `el_emit` and `pl_run`. Point it at a work-in-progress strategy that
uses a keyword this build has not ported yet:

```bash
mkdir -p /tmp/pl_partial_demo
cat > /tmp/pl_partial_demo/wip_strategy.txt <<'EOF'
Value1 = Average(Close, 10);
Value2 = SomeUnportedStudy(Close, 5);
Value3 = AnotherUnportedStudy(High, 3);
EOF
# Strict (default) mode: report every unsupported keyword, write nothing, exit 2.
pl_transpile /tmp/pl_partial_demo/wip_strategy.txt -o /tmp/pl_partial_demo/wip.py \
    || echo "refused (exit $?)"
test ! -f /tmp/pl_partial_demo/wip.py && echo "confirmed: no Python written"
```

The report on stderr names both unsupported keywords, each with its line:

```text
pl_transpile: cannot transpile '/tmp/pl_partial_demo/wip_strategy.txt': 2 unimplemented EL keyword(s):
  'someunportedstudy' at line 2
  'anotherunportedstudy' at line 3
no output written (strict mode). Re-run with --partial to emit execution-time stubs for incremental porting.
```

The strict default is all-or-nothing: either the whole strategy transpiles or
nothing is written. A valid strategy is unaffected.

## Partial transpile mode

Partial mode is an opt-in escape hatch for **incremental porting**. With the
`--partial` flag, `pl_transpile` produces a Python file even when some keywords are
unsupported. Each unsupported construct becomes a **stub that raises the moment it
is evaluated** — never a made-up default value. The file opens with a loud
watermark that names every stub, and the same list is printed to stderr.

**Do not trust results from a partial transpile.** The output is **not faithful**
to the source strategy. A stubbed line either raises when it runs or was simply
never reached, so any backtest against partial output is meaningless. Partial mode
exists only to let you port a strategy piece by piece and exercise the parts that
are already covered — nothing more.

```bash
# Re-uses the work-in-progress strategy written in the section above.
pl_transpile --partial /tmp/pl_partial_demo/wip_strategy.txt \
    -o /tmp/pl_partial_demo/wip_partial.py
head -9 /tmp/pl_partial_demo/wip_partial.py
```

The generated file begins with the watermark (the stderr manifest carries the same
list):

```text
# ======================================================================
# PARTIAL TRANSPILE — NOT FAITHFUL
# 2 unimplemented construct(s) replaced with execution-time stubs.
# do not trust backtest results
# Each stub raises UnimplementedKeywordError when evaluated.
# Unimplemented constructs:
#   'someunportedstudy' at line 2
#   'anotherunportedstudy' at line 3
# ======================================================================
```

If you pass `--partial` to a strategy that has no unsupported keywords, you get one
acknowledgement line followed by output identical to the strict transpile — partial
mode never changes a strategy the transpiler already handles in full.

## Verification tiers

The project is validated by layered gates. The **public tier** is green
out-of-the-box on a fresh clone with no private data; the **capture-backed tier**
needs per-bar MultiCharts captures you attach yourself (see
[docs/CAPTURES.md](docs/CAPTURES.md)).

| Tier | Command | Needs captures? |
|---|---|---|
| Public (portable) | `python3 tests/run_public_tier.py` | No — capture-dependent members skip cleanly |
| Full regression gate | `python3 tests/verify_all.py` (`--fast` for a quick pass) | Yes — GT parity + reverse gates skip without them |
| Round-trip gate | `python3 tests/verify_roundtrip.py` | Yes |
| Two-way gate | `python3 tests/verify_twoway.py` | Yes |
| Full-range partitioned gate | `python3 tests/verify_full_range.py` | Yes |

Every capture-dependent gate prints a one-line pointer to
[docs/CAPTURES.md](docs/CAPTURES.md) and returns success when its captures are
absent, so nothing in a captures-less clone reports a false failure.

## EL keyword catalog

The distilled EasyLanguage / PowerLanguage keyword catalog is the redistributable
replacement for the copyrighted MultiCharts reference corpus (that corpus is
third-party copyrighted content and is **not** included in this distribution or the
installable wheel). It ships as two machine-readable
data files in the package —
`pl_transpiler/tools/pl_signatures.jsonl` (per-keyword `returns` / `min_arity` /
`zero_arg_property`) and `pl_transpiler/tools/mc_verified_builtins.txt` (builtins
confirmed to have a real keyword-reference entry) — and is exposed through the small
`pl_transpiler.catalog` query API.

### As a library

```python
from pl_transpiler import catalog

# Look up a keyword's record (case-insensitive); None if unknown.
print(catalog.get("DayFromDateTime"))
# -> {'keyword': 'DayFromDateTime', 'returns': 'numeric', 'min_arity': 1,
#     'zero_arg_property': False, 'verified': True}

# Minimum argument count + whether it is a zero-arg property (called without parens).
print("arity:", catalog.arity("BarInterval"))        # -> (0, True)
print("returns:", catalog.returns("DayFromDateTime"))  # -> numeric

# Is it an MC-verified builtin? (works even for reserved words without a signature)
print("datetime verified:", catalog.is_verified("datetime"))  # -> True

# Substring search returns a sorted list of names.
print("first date match:", catalog.search("date")[0])  # -> Arw_GetDate

# Derived coverage counts (never hardcoded).
print("coverage:", catalog.coverage())
# -> {'signatures': 481, 'verified_builtins': 190, 'verified_with_signature': 103}
```

### From the command line

```bash
# Print a keyword record as JSON:
python3 -m pl_transpiler.catalog BarInterval

# Substring search across keyword names:
python3 -m pl_transpiler.catalog --search date

# Catalog coverage summary:
python3 -m pl_transpiler.catalog --coverage
```

## Limitations

Please read these before relying on the project — they are deliberate scoping
choices, stated plainly.

- **The headline parity captures are not included.** The per-bar MultiCharts
  ground-truth captures that back the exact-match parity gates are private and are
  **not** shipped. The capture-dependent tests **skip cleanly** on a fresh clone;
  you can attach your own captures to run them (see
  [docs/CAPTURES.md](docs/CAPTURES.md)).
- **This tree is the library, tests, and docs.** Internal build tooling is not
  shipped.
- **Validation scope is intraday index futures only.** Correctness is verified
  against **1-minute @NQ and @ES** bars. Behavior on other instruments, other
  timeframes, or non-futures data is **untested** — treat it as unverified.
- **The Python side is a bounded mirror dialect, not arbitrary Python.** The
  reverse direction reads back only the specific clean Python that the forward
  transpiler emits. General, hand-written Python is out of scope.
- **Everything is fail-loud.** An unsupported EL construct, or Python outside the
  mirror dialect, **raises** rather than guessing. The tools would rather stop than
  emit plausible-but-wrong output.
- **Fail-loud assumes valid EasyLanguage input.** Unimplemented-keyword detection is
  fail-closed (default-deny): a bare name is emitted verbatim only when it is provably
  bound at runtime, and every other unimplemented construct raises (strict) or becomes a
  raising stub (`--partial`). One residual gap is known and requires **input EasyLanguage
  itself rejects**: *assigning to* a read-only performance/report keyword that is not in
  the bundled keyword catalog — e.g. `AvgWinTrade = 5; Value1 = AvgWinTrade;`. EL forbids
  writing to these keywords, so this never occurs in valid source or in a round-trip of
  valid source; but because the transpiler must also accept flattened `x = 0; y = x`
  locals (which are indistinguishable from a keyword write without a complete read-only-
  keyword registry), such a write is treated as a local rather than failing loud.
  Catalog-known keywords are still caught; the uncatalogued few are the residual.

## Licensing

This project is licensed under the **Apache License, Version 2.0**. See
[LICENSE](LICENSE) for the full text and [NOTICE](NOTICE) for attribution.

**Third-party reference materials carve-out.** MultiCharts / TradeStation
documentation and reference content (any files under `resources/`, `db/`, and any
PDF keyword-reference document) are **third-party copyrighted** materials **not**
owned by this project's copyright holder. They are **NOT** covered by the Apache
2.0 grant; all rights remain with their respective owners. **This public distribution
does not include these materials** — they are **excluded from the repository** and from
the **installable wheel** (the built package contains none of them). See
[NOTICE](NOTICE) for the exact wording.

**Trademarks and affiliation.** PowerBridge is an independent open-source project. It
is not affiliated with, endorsed by, or sponsored by TradeStation Technologies, Inc. or
MultiCharts (MCT Limited). TradeStation and EasyLanguage are registered trademarks of
TradeStation Technologies, Inc.; MultiCharts is a registered trademark of MCT Limited;
PowerLanguage is a product name of MultiCharts. These names are used only to describe
compatibility and interoperability.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests are welcome.
Please open an issue before a large change.

## Provenance

This repository starts at a single commit. History before that is not included.
