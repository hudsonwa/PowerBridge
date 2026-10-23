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

# Design notes

This page records the engineering decisions behind PowerBridge and what each one
costs. It is for a reader who wants to judge the project, not to use it. Usage is
in the [README](../README.md), the module map in [ARCHITECTURE.md](ARCHITECTURE.md),
and the test tiers in [verification.md](verification.md).

## The problem

Trading strategies written in MultiCharts PowerLanguage / EasyLanguage (EL) only
run inside the vendor's platform. Porting them to Python by hand is slow, and the
result is hard to trust: a port that differs by one float32 tie or one fill-timing
rule produces a plausible trade list that is simply wrong, and nobody notices.

PowerBridge does three things from one shared representation:

1. transpiles EL to Python (`pl_transpile`, `transpile()`);
2. emits EL back, from the AST or from a bounded Python dialect (`el_emit`);
3. backtests EL over OHLCV bars and reduces the result to a trade digest
   (`pl_run`, `pl_oracle`).

The goal that shapes every decision below: **reproduce MultiCharts' fills exactly,
or refuse.**

## Architecture

```text
EL source -> lexer -> parser -> semantic -> codegen -> Python -> bar loop + FillEngine
                        |                                          |
                        +-> ir.serialise_ir -> Rust kernel ---------+-> fills -> digest
mirror Python -> py_front -> AST -> codegen_el -> EL (checked by mc_ground_check)
```

| Stage | Where | Notes |
|---|---|---|
| Lexer | `pl_transpiler/lexer.py` | Case-insensitive tokens; malformed input raises. |
| Parser | `parser.py`, `_parser_stmts.py`, `_parser_expr.py`, `parser_tables.py` | Produces a plain-dict AST. Unknown keywords are collected across the whole file and reported together. |
| Checks | `semantic.py` | Post-parse rules, such as writes to read-only report words. |
| Codegen | `codegen.py`, `codegen_expr.py`, `codegen_stmt.py`, `codegen_tables.py` | Emits Python that calls `pl_transpiler.runtime`. |
| Runtime | `pl_transpiler/runtime/` | Builtins, float32 narrowing (`_rt_core.f32`), history, indicators (`_rt_indicators.py`, `_rt_trend.py`), bar tape. |
| Engine | `bar_loop.py`, `engine.py`, `_engine_stops.py`, `_engine_report.py` | Drives the strategy one bar at a time; `FillEngine` owns position, orders and P&L. |
| Reverse | `py_front.py`, `_py_front_common.py`, `_py_front_expr.py`, `codegen_el.py`, `_codegen_el_layout.py`, `reverse.py` | Mirror-dialect Python to AST to EL. |
| Kernel | `accel/`, `ir.py`, `runtime/accel.py` | Optional Rust reimplementation of the loop and engine. |
| Oracle | `pl_transpiler/tools/pl_oracle.py` | Runs a strategy and prints a SHA-256 over the canonical fill list. |

The AST is a plain dict rather than a class hierarchy. It diffs easily, serialises
deterministically for the kernel (`ir.serialise_ir` validates it against a schema
first), and round-trips through `py_front`. The cost is no static typing on nodes;
schema checks at the IR boundary and round-trip tests (`tests/test_ir_roundtrip.py`,
`tests/test_ast_retention.py`) cover that gap.

Codegen has three dialects from the same AST: *runnable* (the default, used by
`pl_oracle`), *trace* (adds per-bar records for `pl_run`) and *mirror* (readable
Python with no cross-bar state, accepted only by `py_front`). The bar loop refuses
mirror code (`tests/test_transpile_default_dialect_guard.py`), so a reader cannot
mistake the readable form for a runnable one.

## Key decisions

### Strict by default, lenient only on request

Unsupported syntax stops transpilation; the parser lists every unknown keyword with
its line in one pass and writes nothing (`errors.UnimplementedKeywordError`).
At run time, a builtin that is not implemented is a *loud stub*
(`runtime/_loud_stub_names.py`): calling it stops the run with the builtin's name
and first source line.

- **Why:** a backtest that runs and is slightly wrong is worse than an error.
- **Cost:** some real strategies stop on a builtin that does not affect trades. For
  example, a real-world strategy that timestamps a CSV export with a clock builtin stopped when
  strict became the default (PR #31).
- **Escape hatches:** `--partial` (transpile with raising stubs) and `--lenient` /
  `PL_STRICT=0` (stubs return 0 or `''`, and the run ends with a `WARNING` naming
  them). Both are labelled as not faithful. Cosmetic builtins (drawing, sound,
  print) never stop a run. Tests: `tests/test_strict_default.py`,
  `tests/test_failloud.py`, `tests/test_fail_loud_gaps.py`, `tests/test_partial_mode.py`.

### Float32 semantics

EL stores prices and numeric variables in single precision. A crossover that ties
in float32 can be a strict inequality in float64, which flips a trade. PowerBridge
narrows exactly where EL would:

- Python: `runtime/_rt_core.f32` rounds through IEEE-754 binary32. Codegen wraps
  current-bar OHLCV and numeric `Vars` in it; literals are folded the same way
  (`tests/test_fold_literal_f32.py`).
- Rust: all values stay `f64` and pass through one function, `narrow_f32` in
  `accel/src/types.rs`, called only where Python calls `f32()`. Native `f32`
  arithmetic would round after every operation, which neither EL nor the Python
  path does.
- Per-bar comparisons against captures use a fixed `1e-6` tolerance
  (`tests/golden_diff.py`), which is never loosened.

A missing or extra narrowing site is a silent divergence, so there is one narrowing
function per engine and the digests below catch any drift.

### Per-call-site function state

EL series functions (`XAverage`, `RSI`, `Highest(...)[n]`) carry state between bars,
and two calls with different arguments must not share it. The runtime keeps
incremental indicator state keyed by function, series identity and length, and a
lagged call `Func(series)[n]` gets its own numbered site (`_pb_lag(site, series, n)`
in `runtime/_rt_indicators.py`, numbered by `codegen_expr.py`).

- **Why:** the earlier form passed a fresh slice each bar. That was O(bars²), and a
  freed slice could hand its `id()` (and cached state) to another call, so
  `XAverage(Low,10)[1] + XAverage(High,10)[1]` could read the wrong state (fixed in
  PR #97, with a regression test against a from-scratch recompute).
- **Cost:** caches are process-global and must be cleared between runs
  (`reset_indicator_caches()`).

### Bar loop and fill model

`bar_loop.py` calls the generated strategy once per bar; `engine.py` (`FillEngine`)
applies EL order timing: `next bar at market` fills at the next open,
stop/limit orders fill on the next bar if its range reaches the price,
`this bar on close` fills at the signal bar's close, and money-management stops are
checked inside the bar assuming the extreme nearest the open comes first.

Position is one signed contract count; a reversal is two fill legs. P&L is price
difference × `bigpointvalue` × contracts, with no commission or slippage.
Instrument constants come only from an explicit `InstrumentConfig`
(`runtime/instrument_config.py`); the kernel has no defaults and raises on a missing
field, so the two engines cannot disagree about tick size or point value.

The engine's position and P&L columns are verified exact against private MultiCharts
captures on four order-mode configurations of about 200,000 bars each, reversals
included. Intrabar paths beyond the open-nearest-extreme rule are not modelled.

### Standard library only

`pyproject.toml` has `dependencies = []`. The transpiler, emitter, runtime and test
gates import nothing from PyPI.

- **Why:** the emitted Python is meant to be read and audited, and a backtest that
  depends on a numeric library's version can change answers on upgrade. A
  zero-dependency install also has no build step and a small supply-chain surface
  ([SECURITY.md](../SECURITY.md)).
- **Cost:** no NumPy vectorisation. Speed comes from the optional kernel instead.

### Python and native parity through digests

The Rust kernel (`accel/`, imported as `pl_transpiler._accel`) reimplements the bar
loop, `FillEngine` and an interpreter for the serialised IR. It makes one FFI call
per run (`run_program`), because a per-bar callback would cancel the speed-up, and
it runs parameter sweeps across threads with results identical to a sequential run.

The kernel is a speed-up only and must never change an answer:

- If the module is missing, `import pl_transpiler._accel` raises `ImportError` and
  the Python path runs.
- If the kernel meets a construct it cannot run faithfully, it raises
  `NotImplementedError` with `ACCEL_FALLBACK_REQUIRED` (`accel/src/lib.rs`), and the
  caller (`runtime/accel.py`) reruns on Python.
- If its `FILL_LOG_VERSION` differs from the core's, it is refused (PR #79).

Every fallback prints one `note:` line on stderr (and logs it from the library), so
it is never silent. Strategies that declare arrays currently fall back this way.

Parity is checked by `pl_oracle`: a SHA-256 over a version header
(`digest_version: 2`) plus one canonical line per fill (side, signal, date, time,
price, contracts). Every change in signed position is a fill, so reversals and
`Set*` exits are covered. Running with and without `--python` must give the same
digest on `examples/` and `ground_truth/GT2*`; `tests/conformance_corpus.json`
runs the same differential check per program (`tests/test_accel_conformance.py`).

- **Cost:** every engine change is made twice. The Python path is kept as the
  readable reference; the Rust path is the fast one.

## Verification

| Tier | What it shows | Who can run it |
|---|---|---|
| Public tier (`tests/run_public_tier.py`, `pytest -m public_tier`) | Parser, codegen, runtime, oracle and a mutation pre-flight, on synthetic bars | Anyone, no network or captures |
| Full `pytest -q --timeout=60` | Unit, regression, conformance and README-example tests | Anyone; native and capture tests skip with a message when absent |
| Native vs Python digests | The kernel and Python give the same fill list | Anyone who builds `accel/` |
| Catalog verification levels | How much evidence backs each EL keyword | Anyone (`catalog.verification()`) |
| Private parity gate | Per-bar values and fills match MultiCharts | Maintainer only |

**Synthetic fixtures.** `examples/NQ_sample_bars.csv` and the bars used by tests are
synthetic. The repository contains no licensed market data.

**Catalog levels.** Each keyword in `tools/pl_signatures.jsonl` carries a
`verification` field generated from PowerBridge's own code and tests
(`tools/gen_catalog_fields.py`; a test fails if the file is stale):

- `verified:capture-strategy` (222): used by a strategy whose per-bar outputs match a
  MultiCharts capture;
- `verified:test` (32): pinned by a PowerBridge test;
- `documented-unverified` (251): known keyword, behaviour not independently checked;
- `n/a` (60): syntax words.

The [keyword reference](reference/README.md) shows this level on every entry, so a
reader can tell verified behaviour from documented behaviour.

**The private gate, and why it is private.** MultiCharts parity is checked against
per-bar captures exported from MultiCharts over licensed market data. That data
cannot be redistributed, so the captures are not in the repository and the tests
that need them skip on a clone ([CAPTURES.md](CAPTURES.md) shows how to attach your
own). Only a checksummed manifest is shipped. The public claims that rest on this
gate are stated in [LIMITATIONS.md](LIMITATIONS.md): 1-minute @NQ and @ES, and four configurations of a
real-world multi-entry strategy with 4,278 of 4,278 fills identical. A reader can
reproduce the digest machinery and the Python/native agreement, but not the
MultiCharts comparison itself without their own captures.

Behaviour that has only been read from EL documentation and not observed in a
capture is labelled "documented behaviour, unverified" wherever it appears.

## Limitations and non-goals

- **Python to EL is mirror-only.** `py_front` reads PowerBridge's own mirror
  dialect, not arbitrary Python ([LLM_DIALECT.md](LLM_DIALECT.md) describes the
  bounded subset). Emitted EL is checked by `mc_ground_check`; a nonzero `el_emit`
  exit means it is not known to compile.
- **Validated scope.** 1-minute bars on the instruments above. Other instruments
  and timeframes are untested.
- **Execution model.** No commission, no slippage, fixed session end. MaxBarsBack
  warm-up is opt-in (`--max-bars-back`, default 0).
- **Entries.** Same-direction entries add by default; `--max-entries-per-direction 1`
  matches MultiCharts' documented default (unverified). `Sell from entry("A")` with
  two entries open closes the whole position; the documented EL meaning differs and
  is tracked as unverified (#47) until a capture exists.
- **Unimplemented builtins** stop the run rather than return a guess.
- **Kernel coverage.** Arrays fall back to Python; `pl_run` trace rows use Python
  for windowed, `--max-bars-back` and entry-capped runs.
- **Out of scope:** the package has no live-trading, broker or charting code; it
  backtests and translates only.

## How the project is developed

The git history shows the process:

- Changes land as one issue, one branch and one squash-merged pull request; commit
  subjects carry the PR number, for example `Add opt-in --max-entries-per-direction (#122)`.
- CI (`.github/workflows/ci.yml`, `accel.yml`) runs the public tier, pytest, ruff,
  the README examples and, for the kernel, `cargo fmt`, `clippy` and tests.
- Changes to the engine, codegen, parser or runtime also go through the private
  parity gate before merge.
- Commits are co-authored by an AI coding assistant (`Co-Authored-By: Claude` trailers).
  [AGENTS.md](../AGENTS.md) and [docs/dev/agent-guide-details.md](dev/agent-guide-details.md)
  are the instructions such tools follow, and they restate the same rules as
  this page: fail loud, byte-identical refactors, fixed float32 tolerance, digest
  parity, synthetic data only.
- The keyword reference and catalog have a provenance rule
  ([CONTRIBUTING.md](../CONTRIBUTING.md)): facts come from PowerBridge's code, tests
  and observed behaviour, never from vendor documentation.

The safety of AI-assisted changes rests on the same checks as any other change:
pinned digests, the Python/native comparison, and the private parity gate. A change
that alters a digest without an intended behaviour change does not merge.
