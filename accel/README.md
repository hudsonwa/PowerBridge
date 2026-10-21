# accel — PowerBridge native kernel extra

Optional PyO3 extension. Frozen names (do not change):

| thing | name |
|---|---|
| extra | `accel` |
| native extension module | `_accel` |
| import | `pl_transpiler._accel` |

The crate lives in this directory (`accel/` at the repo root), not inside
`pl_transpiler/`.

The crate contains a native bar loop and `FillEngine` core: next-bar market,
limit, and stop orders; this-bar-on-close queuing; long/short position state;
PnL; open-relative intrabar ordering; money-management stop, target,
breakeven, percent-trailing, and session-exit path walks. Rust unit tests
exercise those pieces without crossing into Python.

## Strategy-semantics policy: fail-loud fallback

The crate deliberately chooses the allowed **fail-loud fallback** approach.
`run` validates and marshals all seven columns in one FFI call, then raises
`NotImplementedError` with an `ACCEL_FALLBACK_REQUIRED` prefix. It never
returns an empty or partial trade list.

The frozen call has no EL source, AST, strategy identifier, instrument
configuration, or Data2 column. The reference strategy itself needs aligned Data2 closes.
Consequently, this call cannot distinguish two strategies over the same tape.
Hard-coding the reference strategy or silently running no strategy would violate the
fail-loud contract. The Python wiring owner must catch this refusal and run
the existing Python path. A future owner-approved strategy-bearing contract
can connect a generated Rust strategy to the already-native loop.

## Build

System Python is PEP 668-blocked. Use a venv; do not `pip install` into
system Python.

```bash
cd accel
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
# cargo / rustc / maturin on PATH (~/.cargo/bin)
maturin develop --release
# Import from a cwd that is NOT the repo root: the real `pl_transpiler`
# package on the repo path would otherwise shadow this extra unless the .so
# is installed next to it.
python -c "import pl_transpiler._accel as a; print(a.ping())"  # 42
```

`cargo build --release` from `accel/` also compiles the `cdylib`. On macOS
that needs the `dynamic_lookup` rustflags in `.cargo/config.toml` (maturin
passes the same flags).

`python/pl_transpiler/` is a PEP 420 namespace marker so maturin 1.15 places
the extension at `pl_transpiler/_accel*.so`. It has no `__init__.py` and
must not grow a second Python implementation.

## Exports

- `ping() -> int` — trivial FFI probe. Always returns `42`.
- `run(open, high, low, close, volume, date, time) -> list[dict]` — frozen
  bars-in / trades-out signature. It validates equal-length numeric columns,
  then raises the fail-loud fallback described above because no strategy is
  present. It never returns a computed result under the current contract.
  The implemented native fill conversion is frozen to `FillEngine.fills` /
  `pl_oracle`: `{type, signal, date, time, price, contracts}`.

One FFI call per run is the only viable shape. A per-bar callback is
forbidden: it destroys the gain this extra exists to capture.

## Native numeric model

Kernel arithmetic is stored as Rust `f64`. In the implemented trade/fill
fields, `narrow_f32` is called only at the corresponding Python `f32()` sites:
fill normalization, weighted-entry intermediates/results, conditional and
on-close fill prices, remaining FIFO average entry, short-close price, tick
snapping, and a newly stored maximum drawdown. Rust unit tests include a
regression vector where naïve all-`f32` arithmetic differs from these explicit
call sites.

## Still required for an active native strategy

- An owner-approved way to supply strategy semantics and required auxiliary
  data without changing or ambiguously overloading the frozen call.
- Python-side fallback wiring and packaging, owned outside this crate.
- Closed-position/GTA6 introspection ledgers, which are not needed by the
  currently implemented explicit-order fill list but would be required by a
  native strategy that reads those reserved words.
- Differential trade-list fidelity for any strategy implementation before
  enabling it. The reference strategy's full-tape oracle has not been run against this call.
