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

# Architecture

This page explains how PowerBridge is put together and why. It is written for
contributors. For usage, see the [README](../README.md). For the native kernel's
interface, see [ACCEL.md](ACCEL.md) and
[dev/ACCEL-INTERFACE-V2.md](dev/ACCEL-INTERFACE-V2.md).

## At a glance

```text
EL source ─► lexer ─► parser ─► semantic ─► codegen ─► Python strategy ─► runtime (bar loop + fills)
                                    │                                                │
                                    └─► ir.serialise_ir ─► native Rust kernel (accel/) ┴─► trade digest
```

Each stage is a module in `pl_transpiler/`; `runtime/` holds the bar loop and
the fill engine, and `accel/` the optional Rust kernel. The kernel either
reproduces the Python trade list exactly or hands the run back to Python with
a visible note.

How parity is checked is [below](#how-parity-is-established) and in
[verification.md](verification.md); measured speed is in
[ACCEL.md](ACCEL.md#speed).

## The one rule

Every design choice below follows from one rule: **match MultiCharts exactly,
or refuse loudly.** A backtest that runs but gives a slightly different trade
list is worse than an error, because nobody notices it. So we:

- reject EL we cannot reproduce at transpile time;
- stop a run when it reaches a builtin we have not implemented;
- make the native kernel either reproduce the Python trade list bit for bit or
  hand the run back to Python with a visible note.

## Repository layout

`pl_transpiler/` is the package (`runtime/` holds the bar loop and fill engine,
`tools/` the console commands), `accel/` the optional Rust kernel, `tests/` the
suite and gates, `tools/` maintainer scripts, `examples/` and `ground_truth/`
the sample bars and the EL conformance corpus. `tests/fixtures/emitted/ground_truth/` holds
EasyLanguage that `el_emit`'s emitter (`codegen_el.emit_el`) produced from the
`ground_truth/` programs at the 0.2.0 export; no script in the tree regenerates
it, and `tests/test_ir_roundtrip.py` parses it as extra corpus input.

## Pipeline

```text
EL source ──lexer──► tokens ──parser──► AST ──semantic──► AST ──codegen──► Python
    ▲                                    │                                   │
    └────────── codegen_el (el_emit) ◄───┴──── py_front ◄─── mirror dialect ─┘
                                         │
                                         └── ir.serialise_ir ──► native kernel
```

Each stage is its own module in `pl_transpiler/`:

| Stage | Module(s) | Notes |
|---|---|---|
| Lexing | `lexer.py` | Case-insensitive EL tokens. |
| Parsing | `parser.py`, `_parser_stmts.py`, `_parser_expr.py`, `parser_tables.py` | Produces a plain-dict AST. Unknown keywords are collected, not skipped. |
| Validation | `semantic.py` | Post-parse checks, such as writes to read-only report words. |
| Python codegen | `codegen.py`, `codegen_expr.py`, `codegen_stmt.py`, `codegen_util.py`, `codegen_tables.py` | Emits Python that calls `pl_transpiler.runtime`. |
| IR | `ir.py` | `serialise_ir` checks the AST against a schema and encodes it deterministically for the kernel. |
| Reverse | `py_front.py`, `_py_front_common.py`, `_py_front_expr.py`, `codegen_el.py`, `_codegen_el_layout.py`, `reverse.py` | Mirror-dialect Python → AST → EL (`el_emit`). |

The AST is a plain dict on purpose. It is easy to diff, to serialise for the
Rust kernel, and to round-trip.

Codegen has three output dialects. They share one AST, so the same strategy
gives the same trades whichever dialect you pick:

- **trace**: cross-bar `_state` plus a per-bar `_trace` record. `pl_run` uses it
  to print per-bar columns.
- **runnable (persist, no trace)**: cross-bar `_state` without per-bar stores.
  `pl_oracle` uses it, since only the fill list matters there. This is the
  default of both `transpile()` and `pl_transpile`.
- **mirror** (`transpile(src, dialect="mirror")`, `pl_transpile --no-persist`):
  clean, readable Python without cross-bar `_state`, so it is not runnable;
  `run_bar_loop` refuses it with a one-line error. This is the only dialect
  `py_front` accepts, which is why the Python → EL direction reads our own
  output and not arbitrary Python.

## Bar loop and fill engine

`bar_loop.py` drives the generated strategy function one bar at a time.
`engine.py` (`FillEngine`) owns position, orders and P&L. Order timing follows
the EasyLanguage model:

- `next bar at market` is queued on the signal bar and fills at the **next
  bar's open**.
- `next bar at <price> stop/limit` fills on the next bar if that bar's range
  reaches the price.
- `this bar on close` fills at the signal bar's close. It is applied before the
  next bar is processed.
- Money-management stops (`SetStopLoss`, `SetProfitTarget`, `SetBreakEven`, the
  trailing stops, `SetStopContract`) are checked inside each bar;
  `SetExitOnClose` closes the position at the session's last bar. For excursions we assume the extreme nearest the open is reached first
  (O→H→L→C or O→L→H→C).

Position is a signed contract count. `Buy` while short reverses (two fill legs),
and `Sell` / `BuyToCover` only close. There is no pyramiding cap. P&L is the
fill-price difference × `bigpointvalue` × contracts, with no commission or
slippage. Instrument constants come from an explicit `InstrumentConfig`, never
from globals. `tick` is used only to round money-management prices to the
price grid.

## Numeric model: why f32 and f64

EasyLanguage stores prices and numeric variables in single precision. A
crossover that is a tie in float32 can be a strict inequality in float64, and
that flips a trade. So we narrow exactly where EL would:

- In Python, `runtime._rt_core.f32` rounds through IEEE-754 binary32.
  Codegen wraps current-bar OHLCV and numeric `Vars` in it. Self-referential
  accumulators stay double, where that is what matches.
- The Rust kernel keeps every value as `f64` and narrows only through one
  function, `narrow_f32` in `accel/src/types.rs`. That function is called only
  where the Python engine calls `f32()`. We avoid native `f32` arithmetic on
  purpose, because Rust would then round after every operation, which is not
  what the Python path (and EL) does. `narrow_f32` is bit-identical to
  `ctypes.c_float(x).value`.

If a narrowing site is missing or extra, the result is a silent trade
divergence. That is why there is a single narrowing function, and why the
digest tests below exist.

## The native kernel

`accel/` is an optional PyO3 extension, `pl_transpiler._accel`, built with
maturin (`./tools/build_accel.sh`). It reimplements the bar loop, the fill
engine and an interpreter for the serialised IR:

- `run_program(program_ir, series, data2, config)` makes one FFI call per run.
  There are no per-bar callbacks, because crossing the boundary once per bar
  would cancel the speed-up.
- Sweeps (many input sets over one tape) compile the program once, specialise
  it per input vector, and can run across threads with the GIL released. The
  results are bit-identical to a sequential run.
- `config` has no defaults on the Rust side. A missing field raises, so the
  kernel can never quietly use different instrument constants from Python.

The caller decides on fallback (`pl_transpiler/runtime/accel.py`). If the
module is missing, the kernel refuses a construct, or its `FILL_LOG_VERSION`
differs from Python's, the run uses the Python engine. The CLIs print one
`note:` line on stderr and the library logs it, so a fallback is never silent.
The core package stays pure-stdlib with no build step. The kernel is purely a
speed-up and never changes an answer.

## How parity is established

`pl_oracle` reduces a run to a **trade digest**: a SHA-256 over a version
header plus one canonical line per fill (type, signal, date, time, price,
contracts), in order. Every change in signed position is a fill, so reversals
and `Set*` exits are covered. Two runs agree only if they produced the same
fills in the same order.

What anyone can check from this repository:

- `tests/run_public_tier.py` and `pytest` run on synthetic and shipped sample
  tapes, with no network and no external data.
- Pinned digests for the examples and the `ground_truth/` strategies, checked
  on both the Python path and the native path (`pl_oracle` with and without
  `--python`).
- A differential conformance corpus (`tests/conformance_corpus.json`) that runs
  each program on both engines and requires identical fill lists.

Separately, we validate against private MultiCharts captures. Those are
licensed data and cannot be redistributed, so the tests that need them skip on
a fresh clone. [verification.md](verification.md) explains what each tier
proves.

## Trade-offs and known limits

- **Refuse instead of approximate.** An unsupported keyword stops
  transpilation, and an unimplemented builtin stops the run. `--partial` and
  `--lenient` exist for porting work, but their results are flagged as not
  trustworthy.
- **Two engines, one semantics.** Keeping Python and Rust in lockstep costs
  effort on every engine change. We accept that cost because the Python path
  is the readable audit trail and the Rust path is the fast one.
- **Narrow validated scope.** Validation covers 1-minute bars on the
  instruments listed in [LIMITATIONS.md](LIMITATIONS.md). Other instruments and timeframes are
  untested.
- **Simplified execution model.** There is no commission or slippage,
  MaxBarsBack warm-up is opt-in (`--max-bars-back`, default 0), and the session
  end is fixed. These are documented in
  [USAGE.md](USAGE.md#what-the-engine-assumes) rather than half-modelled.
- **Kernel coverage.** The kernel does not yet run strategies that declare
  arrays (they fall back to Python), and `pl_run`'s per-bar trace rows use
  Python for windowed and `--partial` runs.
- **Mirror-only reverse direction.** Python → EL works only on our own mirror
  dialect.
