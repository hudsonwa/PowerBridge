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

# ACCEL interface v2 — the strategy-bearing contract

This is the refreeze of the optional native fast path: the interface the Rust
kernel (`accel/`) exposes to Python once an EL program can be executed natively.
It is the single source of truth for the names and shapes the FFI layer, the
kernel lanes, and the Python fallback wiring all build against. It supplements
`docs/ACCEL.md` (the decision record) and `types.rs` (the Rust-side contract
invariants); `docs/ACCEL.md` remains authoritative for the *packaging* decision,
and `types.rs` remains authoritative for the *Rust* semantics.

## Frozen public names (unchanged from v1)

| Fact | Value |
|---|---|
| Extra name | `accel` (`pip install .[accel]`) |
| Native extension import | `pl_transpiler._accel` |
| Extra absent | `import pl_transpiler._accel` raises `ImportError`; callers fall back to the Python path loudly |

## The v2 execution entry point

```text
run_program(program_ir, series, data2=None, config=None) -> list[dict]
```

One FFI crossing per run. There are no per-bar callbacks; crossing the boundary
once per bar would destroy the gain this extra exists to capture.

### `program_ir` — `str`

The `serialise_ir` payload produced by `pl_transpiler.ir.serialise_ir`:

```json
{"ir_version": 1, "kind": "pl_transpiler.ast", "ast": { ... }}
```

A reader refuses an unknown `ir_version` / non-`pl_transpiler.ast` `kind` / a
non-`program` root loudly (never guessed).

### `series` — `dict`

A column-mapped bar bundle (the `BarTape` columnar layout, the same shape
`pl_transpiler.runtime.bar_tape.ohlcv_columns` produces):

```python
series = {
    "open":   [float, ...],   # length N
    "high":   [float, ...],
    "low":    [float, ...],
    "close":  [float, ...],
    "volume": [float, ...],
    "date":   [int, ...],     # EL integer date (YYMMDD)
    "time":   [int, ...],     # EL integer time (HHmm)
}
```

All seven columns equal length (enforced at the boundary; a mismatch raises).

### `data2` — `dict | None`

Optional aligned Data2 series, same column shape as `series`. This interpreter
does not yet implement Data2; a program that references it, or a supplied
`data2`, is refused loudly. `None` for non-Data2 programs.

### `config` — `dict`

Explicit per-run instrument config. **Every field is required** — `Config` has
no `Default`, and nothing the Python path reads from outside the bars may be
defaulted inside Rust (v2 requirements 2-3):

```python
config = {
    "name": "NQ",                 # str
    "bigpointvalue": 20.0,       # float: $ per full point per contract
    "pointvalue": 0.20,          # float: $ per minimum price move
    "minmove": 25,               # int: minmove units per point
    "pricescale": 100,           # int
    "tick": 0.25,                # float: minimum price increment in points
    "barinterval": 1,            # int: bar interval (1-min = 1)
    "session": {"start": 830, "end": 1515},  # dict of int: EL HHmm
    "commission": 0.0,           # float
    "slippage": 0.0,             # float
    "max_bars_back": 0,          # int
    "warmup_seed": 0,            # int
    "symccy": 1,                 # int
    "symnm": "@NQ#C",           # str
    "symalt": "@NQ#C",          # str
    "descstr": "E-MINI NASDAQ 100",  # str
    "expdate": 1130331,          # int: YYYMMdd EL form
}
```

### `Returns` — `list[dict]`

Ordered fills in the `FillEngine.fills` shape `pl_oracle` hashes:

```python
{"type": str, "signal": str, "date": int, "time": int, "price": float, "contracts": int}
```

This is the trade list the fidelity gate compares against the Python path
bit-for-bit.

### Errors

- **`ValueError`** — malformed caller input: a missing/invalid `config` field, a
  non-mapping `series`, or a column-length mismatch.
- **`NotImplementedError`** — a construct outside the implemented surface (e.g.
  Data2). The caller must fall back to the Python path loudly; the kernel never
  returns a partial or approximate trade list.

## The v1 `run()` refusal is preserved

```text
run(open, high, low, close, volume, date, time) -> NotImplementedError
```

The frozen seven-array call carries no EL program, AST, strategy identifier,
instrument config, or Data2, so it still raises `NotImplementedError` with the
`ACCEL_FALLBACK_REQUIRED` prefix. It was **not** removed, weakened, or re-pointed;
`run_program` is additive and does not change `run`'s behaviour.

## Invariants (restated for the execution path)

- **Bit-exact or refuse.** A native run must reproduce the Python trade list
  exactly — same fills, same order, same count. "Close" is a divergence and a
  defect. `narrow_f32` is the only place kernel arithmetic narrows to `f32`, and
  it is called only where the Python engine calls `f32()`.
- **Fail loud.** Anything the kernel cannot execute faithfully raises; there is
  no fallback and no approximation inside the kernel. The fallback decision is
  owned by the caller.
- **Deterministic.** No clock, timezone, locale, filesystem, or RNG in the
  kernel path.
- **Config is explicit.** Every config field is supplied by the caller; a missing
  field raises at the boundary, never defaults.

## Build & install (clean checkout, no Rust toolchain on the core install)

`tools/build_accel.sh` builds the cdylib (`cargo build --release`) and installs
it next to the in-tree `pl_transpiler` package as `pl_transpiler/_accel*.so`:

```bash
./tools/build_accel.sh
python3 -c "import pl_transpiler._accel as a; print(a.ping())"   # 42
```

It builds against the interpreter pointed at by `$PYTHON` (default `python3`),
which must be the same interpreter that performs the import. The installed
artifact is gitignored; the core `pl_transpiler` build remains pure Python with
`dependencies = []` (the `[accel]` extra binding is unchanged, per `docs/ACCEL.md`).
