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

# PowerBridge agent guide — full reference

Detailed API, CLI and invariant reference for agents and scripts. The short guide is [AGENTS.md](../../AGENTS.md); the human-facing guide is [README.md](../../README.md).

## Public naming

The reference strategy this project is benchmarked against is called **Audit Ref 3**
in every public artefact: issues, pull requests, commit messages, docs, code
comments, constant names and test names. That is the only name that appears in
this repository or on its tracker.

## Package layout

- `pl_transpiler` — the transpiler package (pure standard library, no third-party deps).
  - `transpile` — forward transpiler (EL → Python).
  - `pl_transpiler.reverse` — reverse-transpiler surface (EL/AST/Python → EL).
  - `pl_transpiler.py_front` — the mirror-dialect Python front-end (Python → dict AST).
  - `pl_transpiler.catalog` — query API over the distilled EL keyword catalog.
  - `pl_transpiler.tools.el_emit` — reverse-transpiler CLI (`el_emit`).
  - `pl_transpiler.tools.pl_run` — run-EL-over-bars CLI (`pl_run`).
  - `pl_transpiler.tools.pl_oracle` — exact trade-list digest CLI (`pl_oracle`).
  - `pl_transpiler.runtime.*` — the per-bar execution engine, instrument config, runtime,
    compact bar tape (`bar_tape`: opt-in `.pbar` mmap loader), the native-kernel caller
    (`accel`: the `pl_transpiler._accel` FFI crossing).
- `tests/` — the verification gates (shipped, not packaged into the wheel).
- `ground_truth/`, `emitted/`, `examples/` — EL sources and sample data.

The shared **intermediate representation (IR)** is the parser's **dict AST**: a plain
`dict` with a `type`, a `body`, and retained metadata. Every public conversion is a
composition over this IR.

## Python API contracts

### `pl_transpiler.transpile(pl_source: str, trace: bool = False, partial: bool = False, persist: bool | None = None) -> str`

Convert PowerLanguage/EasyLanguage source to Python source.

- `pl_source`: EL source text.
- `trace`: when `True`, the generated Python records every per-bar variable value into
  a `_trace` dict (used by the runtime/`pl_run`); when `False`, emits the clean
  **mirror-dialect** Python that `py_front` can read back (not a runnable backtest).
- `persist`: cross-bar `_state` container. `None` (default) follows `trace`, so
  existing callers are unchanged. `persist=True` with `trace=False` is the
  **runnable no-trace dialect**: EL `Variables` survive from bar to bar (the
  engine already threads `kwargs['_state']`) but the ~111 `_trace[...]` stores
  per bar are omitted. This is **not** the mirror dialect — `py_front` reads
  `trace=False, persist=False` only. `persist=True, trace=True` is today's
  trace-mode output, byte-identical to `trace=True` alone. persist/trace fold
  a literal-argument `f32()` wrap (`X = 0` emits `x = 0.0`, bit-identical to
  `f32(0)`) so those sites are not re-rounded every bar; non-literal wraps
  stay. The clean mirror dialect is unchanged (`Name = f32(<lit>)`).
- `partial`: opt-in partial mode.
  - `False` (default, strict): all-or-nothing. Any unimplemented keyword raises
    `UnimplementedKeywordError` at transpile time (see below) and returns nothing.
    Byte-for-byte identical to the pre-partial transpiler.
  - `True`: every unimplemented construct is replaced by an execution-time stub
    (`pl_partial_stub(name, line)`) that **raises when evaluated**, and the returned
    source is prefixed with a NOT-FAITHFUL watermark naming every stub. When there
    are **zero** unimplemented constructs the output is one acknowledgement comment
    line followed by the byte-identical strict output.
- Returns: Python source as `str`.
- Raises: `UnimplementedKeywordError` (fail-loud) in strict mode on EL it cannot
  represent — never returns plausible-but-wrong Python.

```python
from pl_transpiler import transpile
py = transpile(open("ground_truth/GT1_functions_indicators.txt").read(), trace=False)
assert isinstance(py, str) and "def strategy" in py
# partial=True on the same valid source: one ack line + byte-identical strict output.
p = transpile(open("ground_truth/GT1_functions_indicators.txt").read(), partial=True)
assert p.splitlines()[0].startswith("# PARTIAL TRANSPILE requested — 0 unimplemented")
assert p.endswith(py)
# persist=True, trace=False: runnable backtest (state lives, no _trace stores).
# Default persist follows trace, so this is a new opt-in — the clean dialect is unchanged.
runnable = transpile(open("ground_truth/GT1_functions_indicators.txt").read(), persist=True)
assert "_state" in runnable and "_trace =" not in runnable
print("transpile OK:", len(py), "chars")
```

### `pl_transpiler.errors` — collect-all transpile diagnostics

```text
UnimplementedKeywordError(Exception)
    .errors : list[{'name': str, 'line': int | None}]
              # every unimplemented keyword, deduped by (name, line),
              # sorted by (line, name) with None lines LAST.
    str(err): one of two message formats (see below).
format_error_lines(errors) -> list[str]   # "'<name>' at line <L>" per entry (line omitted if None)
```

A single **strict** `transpile()` call walks the ENTIRE program and reports EVERY
unimplemented keyword at once — you do not fix them one recompile at a time. Message
formats:

- **exactly one** unimplemented keyword (legacy, byte-compatible):
  `unimplemented EL keyword '<name>' at line <L>`
- **more than one**:
  `cannot transpile: <N> unimplemented keywords: '<n1>' at line <L1>; '<n2>' at line <L2>; ...`

Both **call-form** (`FooKw(Close)`) and **bare** (parenless, e.g. `Value1 = FastD;`
or `Value1 = AvgWinTrade;`) uses of an unported keyword are caught the same way —
neither is ever emitted as an undefined name. Strict mode collects and raises;
partial mode emits an execution-time stub for each.

```python
from pl_transpiler import transpile
from pl_transpiler.errors import UnimplementedKeywordError, format_error_lines
src = ("Value1 = FooKw(Close);\n"                       # line 1: fookw
       "Value2 = BarKw(High) + BarKw(Low);\n"           # line 2: barkw twice -> ONE entry
       "If Close > 0 Then Value3 = FooKw(Open);\n")      # line 3: fookw again (kept: new line)
try:
    transpile(src)
    raise SystemExit("expected UnimplementedKeywordError")
except UnimplementedKeywordError as e:
    assert e.errors == [
        {"name": "fookw", "line": 1},
        {"name": "barkw", "line": 2},
        {"name": "fookw", "line": 3},
    ], e.errors
    assert str(e).startswith("cannot transpile: 3 unimplemented keywords: ")
    assert format_error_lines(e.errors)[0] == "'fookw' at line 1"
# Single unimplemented keyword keeps the legacy one-line message + a 1-element list.
try:
    transpile("Value1 = LoneKw(Close);\n")
    raise SystemExit("expected UnimplementedKeywordError")
except UnimplementedKeywordError as e:
    assert str(e) == "unimplemented EL keyword 'lonekw' at line 1", str(e)
    assert e.errors == [{"name": "lonekw", "line": 1}]
print("collect-all + partial diagnostics OK")
```

`UnimplementedKeywordError` is also importable from `pl_transpiler.codegen` (same
class). Partial mode's watermark and the CLI manifests render the identical entries
via `format_error_lines`, so the strict report, the file watermark, and the stderr
manifest always agree.

### `pl_transpiler.reverse`

Thin, I/O-free wrappers over the verified reverse pieces. All take/return `str` except
`parse_el`, which returns the dict AST.

```text
emit_el(ast: dict) -> str            # dict AST -> canonical EL (AST-only; never reads source text)
parse_el(el_text: str) -> dict       # EL -> dict AST (the shared IR)
el_to_py(el_text: str) -> str        # EL -> clean mirror-dialect Python (transpile, trace=False)
py_to_el(py_text: str) -> str        # mirror-dialect Python -> EL (py_front -> emit_el)
el_roundtrip(el_text: str) -> str    # EL -> AST -> EL (identity round-trip)
```

- `emit_el` raises if the AST contains a node it cannot faithfully emit (fail-loud).
- `py_to_el` raises `pl_transpiler.py_front.PyFrontError` on Python outside the mirror
  dialect.

```python
from pl_transpiler.reverse import parse_el, emit_el, el_roundtrip
el = open("ground_truth/GT1_functions_indicators.txt").read()
ast = parse_el(el)
assert isinstance(ast, dict) and ast["type"]
emitted = emit_el(ast)
assert isinstance(emitted, str) and emitted == el_roundtrip(el)
print("reverse OK")
```

### `pl_transpiler.py_front`

```text
py_to_ast(py_text: str) -> dict      # mirror-dialect Python -> dict AST
PyFrontError                         # exception raised on non-mirror-dialect Python
DIALECT_VERSION: int                 # dialect contract version, asserted against codegen
```

`py_to_ast` is **fail-loud**: any Python construct outside the mirror dialect (e.g.
arbitrary imports, unsupported statements) raises `PyFrontError` rather than being
guessed at.

```python
from pl_transpiler import py_front
assert isinstance(py_front.DIALECT_VERSION, int)
try:
    py_front.py_to_ast("import os\nos.system('nope')\n")
    raise SystemExit("expected PyFrontError")
except py_front.PyFrontError:
    print("py_front fail-loud OK")
```

### `pl_transpiler.catalog`

Query API over the two shipped catalog data files. All lookups are case-insensitive.
Counts are derived from the data, never hardcoded.

```text
get(name: str) -> dict | None                 # record dict (+ derived 'verified' bool), or None
arity(name: str) -> tuple[int, bool]          # (min_arity, zero_arg_property); raises KeyError if unknown
returns(name: str) -> str                     # declared return type; raises KeyError if unknown
is_verified(name: str) -> bool                # True iff name is an MC-verified builtin
search(substring: str) -> list[str]           # sorted keyword names containing substring
coverage() -> dict                            # {'signatures', 'verified_builtins', 'verified_with_signature'}
```

- `get` returns `None` for an unknown keyword (not an exception).
- `arity`/`returns` raise `KeyError` (with a helpful message) for a keyword absent from
  the signature catalog.

```python
from pl_transpiler import catalog
rec = catalog.get("BarInterval")
assert rec["keyword"] == "BarInterval" and rec["verified"] is True
assert catalog.arity("BarInterval") == (0, True)
assert catalog.get("NoSuchKeyword") is None
try:
    catalog.arity("NoSuchKeyword")
    raise SystemExit("expected KeyError")
except KeyError:
    pass
cov = catalog.coverage()
assert set(cov) == {"signatures", "verified_builtins", "verified_with_signature"}
print("catalog OK:", cov)
```

### `pl_transpiler.tools.pl_run`

```text
load_bars_csv(path: str) -> list[dict]
    # Parse an OHLCV CSV into bar dicts (keys: date,time,open,high,low,close,volume).
    # Accepts MultiCharts ASCII (DD/MM/YYYY, HH:MM:SS) or EL-integer (YYMMDD, HHMM) forms.
    # Unchanged: this is still the default loader.

load_bars(path: str) -> list[dict] | BarTape
    # Dispatch: `*.pbar` -> load_pbar (mmap BarTape); anything else -> load_bars_csv.

csv_to_pbar(csv_path: str, pbar_path: str) -> int
    # Parse with load_bars_csv and write a .pbar. Returns bar count.

resolve_trade_from(bars, *, from_date=None, from_bar=None) -> int
    # First tape index that may fill/emit. 0 = no window. from_date and from_bar
    # are mutually exclusive. Past-the-tape bounds return len(bars).

run_el(el_source: str, bars: list[dict] | BarTape, config, *, trace_columns: list[str] | None = None,
       partial: bool = False, row_sink=None, header_sink=None, retain_rows=None,
       from_date=None, from_bar=None, seed_warmup: bool = True)
    -> {'columns': list[str], 'rows': list[list[str]], 'trades': list, 'n_rows': int,
        'n_warmup': int}
    # Transpile (trace=True) and run EL bar-by-bar over supplied bars + instrument config.
    # trace_columns=None -> the first bar's sorted trace keys become the columns.
    # partial=False (default): unimplemented keywords raise at TRANSPILE time, before any
    #   bar runs — byte-identical to before.
    # partial=True: transpiles in partial mode and prints the stub manifest loudly to
    #   stderr first; a stubbed construct RAISES UnimplementedKeywordError at the bar it is
    #   evaluated on (message "partial-mode stub executed ..."). A stub never reached (e.g.
    #   behind a false guard) never fires, and those rows match the strict run with that
    #   line removed. The CLI exposes this as `pl_run --partial`.
    # row_sink / header_sink: optional callables. row_sink(row) is invoked with each
    #   formatted list[str] during the bar loop; header_sink(columns) once when columns
    #   become known. Additive — existing callers unchanged.
    # retain_rows: None (default) retains rows when row_sink is None and drops them when
    #   a sink is given (streaming). Pass True to sink AND keep result['rows'].
    # n_rows: number of bars actually emitted (equals len(rows) when retaining).
    # from_date / from_bar: opt-in window start (EL-integer YYYMMDD, or 0-based tape
    #   index). Default None = full tape. Mutually exclusive. Never implied.
    # seed_warmup: when a window is set, True (default) runs the discarded prefix with
    #   fills suppressed so Once / lookback / Variables stay primed. False drops the
    #   prefix (tape slice) — only equivalent when the trade-list oracle agrees.
    # n_warmup: seeded-prefix length (0 if no window, or if seed_warmup=False).
    # Cell format is unchanged: None / missing -> '0', else str(value).
    # bars may be a BarTape; columns are consumed directly, same trades.
```

`run_el` is **fail-loud** (`strict=True`): any EL surface the runtime cannot reproduce
**raises** rather than being silently skipped. `run_el`/`load_bars_csv` drive the shipped
`pl_transpiler.engine` module (the bar-by-bar engine + FillEngine), so they work from a
bare `pip install` — no source checkout required. TRUST MODEL: `run_el` (like the `pl_run`
CLI) **executes generated Python** on your machine, so only run strategy files you trust;
converting/transpiling alone never executes the input.

```python
from pl_transpiler.tools.pl_run import run_el, load_bars_csv
from pl_transpiler.runtime.instrument_config import get_config
out = run_el(open("ground_truth/GT2_strategy_orders_position.txt").read(),
             load_bars_csv("examples/NQ_sample_bars.csv"),
             get_config("NQ"), trace_columns=["marketpos", "netprofit"])
assert out["columns"] == ["marketpos", "netprofit"]
assert len(out["rows"]) == len(load_bars_csv("examples/NQ_sample_bars.csv"))
assert out["n_rows"] == len(out["rows"])
print("pl_run library OK:", len(out["rows"]), "rows,", len(out["trades"]), "trades")
```

### `pl_transpiler.runtime.bar_tape` — compact mmap bar input

Opt-in alternative to CSV. The on-disk `.pbar` is a little-endian struct-of-arrays
dump of **exactly** the values `load_bars_csv` produces: EL-integer `date`/`time`
(`int32`) and IEEE-754 **binary64** `open`/`high`/`low`/`close`/`volume`. Storing
`float32` would change values and is rejected. `mmap` is the default reader.

This is the bar-**input** layout. Series history buffers stay growing
Python lists — the engine still appends one scalar per bar.

```text
MAGIC = b'PBARS01\n'          # 8 bytes
HEADER = magic + uint32 version=1 + uint32 flags=0 + uint64 n_bars  # 24 bytes
then columns, each contiguous:
  date[n] i32, time[n] i32, open[n] f64, high[n] f64, low[n] f64, close[n] f64, volume[n] f64
BYTES_PER_BAR = 48. Host must be little-endian (fail-loud otherwise).

BarTape : Sequence. tape[i] -> dict (lazy, same keys/types as load_bars_csv).
          tape[a:b] -> BarTape view (no copy). Columns are public attributes.
load_pbar(path) -> BarTape          # mmap, holds the mapping
write_pbar(path, bars) -> n         # bars is BarTape or list[dict]
csv_to_pbar(csv_path, pbar_path) -> n
ohlcv_columns(bars) -> {n, open, high, low, close, volume, date, time}
    # BarTape: existing column views. list[dict]: new lists (today's run_el).
```

Produce a `.pbar` from a CSV (same parse as `load_bars_csv`):

```bash
python3 -m pl_transpiler.runtime.bar_tape csv2pbar bars.csv bars.pbar
```

```python
from pl_transpiler.runtime.bar_tape import csv_to_pbar, load_pbar
from pl_transpiler.tools.pl_run import load_bars_csv, run_el
from pl_transpiler.runtime.instrument_config import get_config
csv_to_pbar("examples/NQ_sample_bars.csv", "/tmp/nq.pbar")
csv_bars = load_bars_csv("examples/NQ_sample_bars.csv")
with load_pbar("/tmp/nq.pbar") as tape:
    assert len(tape) == len(csv_bars)
    for i, b in enumerate(csv_bars):
        assert tape[i] == b
    el = open("ground_truth/GT2_strategy_orders_position.txt").read()
    a = run_el(el, csv_bars, get_config("NQ"), retain_rows=False)
    tape_out = run_el(el, tape, get_config("NQ"), retain_rows=False)
    assert a["trades"] == tape_out["trades"]
print("bar_tape OK:", len(csv_bars), "bars")
```

`pl_run` / `pl_oracle` CLIs accept a `.pbar` in the bars path (detected by suffix).
CSV remains the default for any other suffix.

### `pl_transpiler.runtime.accel` — native-kernel caller

The Python side of the optional native fast path (`pl_transpiler._accel`). Owns
the single FFI crossing and the fallback decision; the kernel never guesses.

```text
run_native(el_source, bars, config, *, data2_bars=None) -> list[dict]
    # -> ordered fills in FillEngine.fills / get_mc_trades shape (NO 'order_no').
    # Exactly one FFI crossing: parse EL -> serialise_ir -> _accel.run_program.
    # data2_bars: optional full second bar series (same shape as `bars`); the
    #   kernel aligns it to Data1 by (date, time).
    # Raises AccelUnavailable (extension not built) or AccelRefused (kernel
    #   NotImplementedError) -- NEVER a partial/approximate trade list.

run_native_trace(el_source, bars, config, *, data2_bars=None)
    -> (columns, rows, fills)
    # The typed trace sink: one FFI crossing runs the tape natively
    # and emits the kernel's register values TYPED (float/int/bool/str), not the
    # pre-formatted 24-column text -- Python owns str(value) rendering.
    # `columns` is the sorted union of bar metadata (`_barnumber`/`_date`/`_time`/
    # `_close`), the engine position columns (`current_contracts`, `entry_price`,
    # `net_profit`, `totaltrades`, ...) and the strategy's variables. `rows` is one
    # typed list per evaluated bar (full tape -- no skip), aligned to `columns`.
    # `fills` is the same ordered FillEngine.fills list `run_native` returns, so a
    # trace run's trade-list digest equals pl_oracle bit-for-bit. Raises the same
    # AccelUnavailable / AccelRefused -- never a partial trace.

accel_available() -> bool
    # True iff `import pl_transpiler._accel` succeeds.

fallback_count() -> int
    # Process-wide number of native-path fallbacks signalled (AccelUnavailable or
    # AccelRefused raised by run_native). A successful native run never increments
    # it, so the differential gate asserts it is unchanged around a run to prove
    # the native path was actually taken (a Python-fallback digest is a false green).
```

Sweep/batch entry points — the prepared path the sweep runner uses:

```text
run_native_inputs(el_source, bars, config, inputs, *, data2_bars=None) -> list[dict]
    # One variant run in isolation with explicit Input overrides (`inputs` is a
    #   name -> number/bool/string mapping). The sweep/batch "solo" reference.

run_native_sweep(el_source, bars, config, variants, *, data2_bars=None, threads=None)
    -> list[list[dict]]
    # K input variants over ONE tape, compiled + marshalled once.
    # `variants` is a list of input-override dicts (one per variant).
    # threads=None -> sequential batch; threads=N -> fan the sweep across N worker
    #   threads (released GIL). `out[i]` is bit-identical to
    #   run_native_inputs(..., variants[i]) -- the solo_digest == batch_digest
    #   invariant (parallelism never changes an answer).
```

The underlying `_accel` FFI additions are `run_program_inputs(program_ir, series,
data2, config, inputs)`, `run_program_batch(program_ir, series, data2, config,
variants)`, and `run_program_batch_parallel(program_ir, series, data2, config,
variants, threads)`. The frozen `run_program` contract is untouched.

### `pl_transpiler.tools.pl_oracle`

Exact trade-list digest. The merge gate for performance work: later changes
must cite the before/after digest. **No digest, no merge.** Does not change the
engine, the dialect, or `run_el`.

```text
run_oracle(el_source, bars, config, *, data2_closes=None, data2_bars=None,
           from_date=None, to_date=None, from_bar=None, seed_warmup=False,
           use_native=False)
    -> {'text', 'fills', 'round_turns', 'closed_positions', 'sha256', 'n_bars',
        'n_warmup', 'records', 'native', 'native_fallback_reason'}
    # transpile(persist=True), run_bar_loop min-emit (no _trace, no retained rows).
    # from_date / to_date: inclusive EL-integer (YYYMMDD). None = unbounded.
    # from_bar: 0-based tape index (mutually exclusive with from_date).
    # seed_warmup=False (default): tape slice — drop the prefix. The fast
    #   path; Audit Ref 3 from 2018-01-01 matches the full tape only because that EL has
    #   zero lookback.
    # seed_warmup=True: keep the prefix, run it with fills suppressed (Once /
    #   lookback / Variables stay primed). A windowed run that disagrees on trades
    #   fails the check, not a speed win.
    # data2_closes: optional list[float] aligned with bars (Close of Data2).
    #   Drives the Python path's per-bar `data2_close`. The native path cannot
    #   reconstruct a full Data2 tape from closes-only and refuses that shape
    #   loudly (then run_oracle falls back to Python).
    # data2_bars: optional Data2 bar series (list[dict] or BarTape). The native
    #   path forwards it to run_native (aligned by (date, time)). The Python
    #   path, when data2_closes is omitted, derives Close-of-Data2 from it the
    #   same way — data2_bars is never ignored. Either spelling is accepted on
    #   the Python path; when both are supplied, data2_closes is used there.
    # use_native=False (default): the pure-Python path, byte-identical to before.
    #   True: try the native kernel first (one FFI crossing) for the full-tape
    #   shape (a Data2 program is served by forwarding data2_bars); a refusal (or a
    #   missing extension) falls back to the Python path LOUDLY (a stderr note) --
    #   never a partial trade list. The native path is correct-but-slower today:
    #   it reproduces the frozen digest but is not yet a speed win over the Python
    #   path.
    # records: FillEngine.get_mc_trades() list (ordered). On the native path the
    #   records are the kernel fills (no 'order_no') and closed_positions is None
    #   (the FFI returns fills, not the closed-position ledger -- neither is in the digest).
    # native: True iff the native kernel produced the digest.
    # native_fallback_reason: str when use_native requested but Python ran, else None.
    # text: canonical four-line digest (always ends with a newline).
    # n_warmup: seeded-prefix length (0 unless seed_warmup ran a prefix).

format_digest(*, fills, round_turns, sha256) -> str
fills_sha256(fills) -> str
    # SHA-256 of ordered canonical fill lines
    #   "{type}\\t{signal}\\t{date}\\t{time}\\t{price.hex()}\\t{contracts}\\n"
    #   {type} is the buy/sell SIDE, not a position label: "EntryLong" = buy side
    #   (Buy, BuyToCover), "ExitLong" = sell side (Sell, SellShort). A SellShort
    #   from flat hashes as "ExitLong". Frozen: every pinned digest depends on it.
fill_trade_types(fills) -> list[str]
    # Position-aware label per fill (EntryLong / ExitLong / EntryShort / ExitShort),
    # replayed from flat. Reversals split into exit records then entry records.
    # Not hashed; same result for native and Python records.
write_fills_csv(path, fills)
    # Readable fill log: order_no,trade_type,side,signal,date,time,price,contracts,
    # position_after. Used by --fills-csv.
window_bars(bars, from_date=None, to_date=None, extra=None)
    -> (sliced_bars, sliced_extra)
data2_closes_from_bars(bars, data2_bars) -> list[float]
    # Close of Data2 aligned onto Data1 by (date, time), last-wins; missing
    # timestamps fall back to Close of Data1. Used when run_oracle is given
    # data2_bars without data2_closes.
```

Two runs on unchanged code produce **byte-identical** `text`. The hash covers
every fill (entry and exit); `round_turns` is `len(fills) // 2` (one contract
per fill record — Audit Ref 3: 7,364 fills / 3,682 round-turns). `closed_positions`
is `len(FillEngine.closed_trades)` and is **not** in the digest (a scale-in
ladder is one closed position but many round-turns). A windowed tape that
disagrees on trades fails the check, not a speed win — Audit Ref 3 is only safe
to **slice** because its EL has zero lookback; that property is *measured* by
comparing digests, not assumed. Strategies that use Once, Close[n], Average, or
a bar-1 cumulative need ``seed_warmup=True`` (or must not be windowed).

```python
from pl_transpiler.tools.pl_oracle import run_oracle
from pl_transpiler.tools.pl_run import load_bars_csv
from pl_transpiler.runtime.instrument_config import get_config
d = run_oracle(open("ground_truth/GT2_strategy_orders_position.txt").read(),
               load_bars_csv("examples/NQ_sample_bars.csv"),
               get_config("NQ"))
assert d["text"].startswith("powerbridge-trade-digest v1\n")
assert d["fills"] == 4 and d["round_turns"] == 2
d2 = run_oracle(open("ground_truth/GT2_strategy_orders_position.txt").read(),
                load_bars_csv("examples/NQ_sample_bars.csv"),
                get_config("NQ"))
assert d["text"] == d2["text"]
# seed_warmup keeps the prefix; default from_date is a tape slice.
seeded = run_oracle(open("ground_truth/GT2_strategy_orders_position.txt").read(),
                    load_bars_csv("examples/NQ_sample_bars.csv"),
                    get_config("NQ"), from_bar=10, seed_warmup=True)
assert seeded["n_warmup"] == 10
print("pl_oracle library OK:", d["fills"], "fills,", d["sha256"][:12])
```

## CLI contracts

### `pl_transpile` (`pl_transpiler.tools.pl_transpile`)

```text
pl_transpile <in_file> [-o OUT] [--trace] [--persist] [--partial]
python3 -m pl_transpiler.tools.pl_transpile <in_file> [-o OUT] [--trace] [--persist] [--partial]

  in_file    : EL/PowerLanguage source file.
  -o OUT     : write Python here (default: stdout).
  --trace    : emit trace-mode Python (per-bar variable capture) instead of the clean dialect.
  --persist  : emit cross-bar `_state` even without `--trace` (runnable no-trace dialect).
               Default follows `--trace`. Does not change the clean mirror dialect.
  --partial  : opt-in partial mode (see below).

  DEFAULT (strict): valid source -> exit 0, Python written. Unimplemented keywords ->
    a complete report (EVERY keyword, name + line) to stderr, NO output file, exit 2.
    All-or-nothing: a strict failure never leaves a partial/plausible-but-wrong file behind.
  --partial: exit 0; watermarked Python written even when keywords are unimplemented, and the
    stub manifest (banner + one line per stub + do-not-trust warning) printed LOUDLY to stderr.
    With zero unimplemented keywords the output is the strict output plus one leading ack line.
  Exit code: 0 on success (including any --partial run); 2 on strict-mode unimplemented
    keywords OR an argparse usage error. Installed console script; runs from any directory
    after `pip install .`.
```

```bash
set -e
cat > /tmp/agents_wip.txt <<'EOF'
Value1 = Average(Close, 10);
Value2 = SomeUnportedStudy(Close, 5);
EOF
# strict: exit 2, no output file written.
rc=0; python3 -m pl_transpiler.tools.pl_transpile /tmp/agents_wip.txt -o /tmp/agents_wip.py || rc=$?
test "$rc" -eq 2
test ! -f /tmp/agents_wip.py
# partial: exit 0, watermarked file written that names the stub.
python3 -m pl_transpiler.tools.pl_transpile --partial /tmp/agents_wip.txt -o /tmp/agents_wip_partial.py
grep -q "PARTIAL TRANSPILE — NOT FAITHFUL" /tmp/agents_wip_partial.py
grep -q "someunportedstudy" /tmp/agents_wip_partial.py
echo "pl_transpile CLI contract OK"
```

### `el_emit` (`pl_transpiler.tools.el_emit`)

```text
el_emit <input> [-o OUTPUT]
python3 -m pl_transpiler.tools.el_emit <input> [-o OUTPUT]

  <input>  : a .py file  -> py_to_el (mirror-dialect Python -> EL)
             any other   -> el_roundtrip (EL -> AST -> canonical EL)
  -o OUT   : write emitted EL to OUT (default: stdout)

  Behaviour: writes canonical EL, then runs mc_ground_check on it.
  Exit code: 0 if the emitted EL is compile-safe; NONZERO (fail-closed) otherwise.
  The mc_ground_check report is printed to stdout.
```

The full name-proof step needs the MultiCharts keyword reference on disk (that
copyrighted corpus is **not** included in this distribution). Without it the step is
skipped and only argument-count/return-type checks run; this can produce a false
failure on order-verb scripts, so prefer indicator inputs there.

```bash
python3 -m pl_transpiler.tools.el_emit ground_truth/GT1_functions_indicators.txt -o /tmp/GT1_emitted.txt
```

### `pl_run` (`pl_transpiler.tools.pl_run`)

```text
pl_run <el_file> <bars_csv> [--config NAME] [-o OUT] [--columns a,b,c] [--partial]
       [--from-date ELINT | --from-bar N] [--slice]
python3 -m pl_transpiler.tools.pl_run <el_file> <bars_csv> [--config NAME] [-o OUT] [--columns a,b,c] [--partial]
       [--from-date ELINT | --from-bar N] [--slice]

  el_file    : EL/PowerLanguage source file.
  bars_csv   : OHLCV CSV (date,time,open,high,low,close,volume) or a .pbar. REQUIRED.
  --config   : instrument config name (default: NQ). The config
               (pl_transpiler/runtime/instrument_config.py) supplies BigPointValue,
               PointValue, MinMove, PriceScale and the symbol-info keywords:
                 Symbol / SymbolName -> symalt / symnm   (NQ: "@NQ#C")
                 Description         -> descstr          (NQ: "E-MINI NASDAQ 100")
                 Category            -> category         (0 = future, the default)
                 SymbolCurrencyCode  -> symccy           (numeric code; 1 = USD, as
                                                          MultiCharts reports it)
                 ExpirationDate      -> expdate          (EL YYYMMDD; 0 = unknown)
               The optional data-stream argument (SymbolName(1)) is ignored: a run
               has one instrument.
  -o OUT     : write the predicted per-bar CSV to OUT (default: stdout).
  --columns  : comma-separated trace columns to emit (default: all trace keys).
  --partial  : opt-in partial mode (run_el partial=True): unimplemented keywords become
               execution-time stubs, the stub manifest is printed loudly to stderr, and a
               stub RAISES UnimplementedKeywordError the moment a bar evaluates it. A stub
               never reached does not fire. Default (no flag): unimplemented keywords raise
               at transpile time before any bar runs. NOT FAITHFUL — do not trust its output.
  --from-date / --from-bar : opt-in window start (EL YYYMMDD, or 0-based tape index).
               Default is the full tape. Seeded warmup (prefix runs, fills suppressed)
               unless --slice. A window that disagrees on trades fails the check.
  --slice    : drop the prefix instead of seeding warmup. Only equivalent when the
               trade-list oracle agrees.

  stdout : the per-bar CSV (unless -o given). Streamed during the bar loop
           (not buffered as list[list[str]] first).
  stderr : a one-line "[pl_run] N bars, M rows, K fills, R round-turns" status line.
  Exit code: 0 on success; 2 on a usage error (missing bars_csv, unknown flag).
  Ships as an installed console script: run `pl_run ...` from any directory after
  `pip install .` (or `python3 -m pl_transpiler.tools.pl_run ...`). No checkout needed.
  TRUST MODEL: pl_run EXECUTES GENERATED PYTHON — only run strategy files you trust.
```

```bash
python3 -m pl_transpiler.tools.pl_run \
    ground_truth/GT2_strategy_orders_position.txt \
    examples/NQ_sample_bars.csv --config NQ --columns marketpos,netprofit -o /tmp/pl_run_out.csv
```

### `pl_oracle` (`pl_transpiler.tools.pl_oracle`)

```text
pl_oracle <el_file> <bars_csv> [--config NAME] [--from-date ELINT | --from-bar N]
          [--to-date ELINT] [--seed-warmup] [--data2 CSV] [--native | --python] [-o OUT]
          [--compare FILE] [--dump FILE] [--fills-csv FILE]
python3 -m pl_transpiler.tools.pl_oracle <el_file> <bars_csv> ...

  el_file      : EL/PowerLanguage source file.
  bars_csv     : OHLCV CSV (date,time,open,high,low,close,volume) or a .pbar. REQUIRED.
  --config     : instrument config name (default: NQ).
  --from-date  : inclusive EL-date lower bound (YYYMMDD). Tape slice unless --seed-warmup.
  --from-bar   : 0-based tape-index lower bound. Tape slice unless --seed-warmup.
  --to-date    : inclusive EL-date upper bound (YYYMMDD). Tape slice only.
  --seed-warmup: run the discarded prefix with fills suppressed (Once/lookback/Variables
                 stay primed) instead of dropping it. Default is slice.
  --data2      : aligned Data2 OHLCV CSV/.pbar; its close column drives data2_close
                 per bar on the Python path, and the full tape is forwarded to the
                 native kernel under --native.
  --native     : use the Rust kernel (one FFI crossing). This is the DEFAULT when
                 pl_transpiler._accel is installed; it falls back to Python with a
                 stderr WARNING when the kernel cannot serve the run (e.g. arrays,
                 windowed runs). Same digest as Python.
  --python     : force the reference Python path.
  -o OUT       : also write the digest to OUT (stdout always gets it).
  --compare F  : exit 1 if the produced digest differs from F (byte-for-byte).
  --dump FILE  : write the canonical fill lines (the hashed payload) to FILE. Their
                 first field is the buy/sell side ("EntryLong" = buy, "ExitLong" = sell),
                 not a position label.
  --fills-csv FILE : write a readable fill log CSV with a position-aware trade_type
                 column (EntryLong / ExitLong / EntryShort / ExitShort). Not hashed;
                 the digest is unchanged.

  stdout : the four-line digest (powerbridge-trade-digest v1 / fills / round_turns / sha256).
  stderr : a one-line "[pl_oracle] N bars, F fills, R round-turns [native|python]" status line.
  Exit code: 0 on success (and on --compare match); 1 on --compare mismatch;
             2 on a usage error (missing bars_csv, unknown flag).
  Ships as an installed console script after `pip install .`.
  Protocol: transpile(persist=True) + run_bar_loop min-emit. Not a backtest report.
  TRUST MODEL: pl_oracle EXECUTES GENERATED PYTHON — only run strategy files you trust.
```

```bash
python3 -m pl_transpiler.tools.pl_oracle \
    ground_truth/GT2_strategy_orders_position.txt \
    examples/NQ_sample_bars.csv --config NQ
```

### `python -m pl_transpiler.catalog`

```text
python3 -m pl_transpiler.catalog <name>          # print the keyword record as JSON (stdout)
python3 -m pl_transpiler.catalog --search <sub>  # print matching keyword names, one per line
python3 -m pl_transpiler.catalog --coverage      # print derived catalog counts

  Exit code: 0 on a successful lookup/search/coverage;
             1 if <name> is not found (message on stderr);
             2 if no argument is given (prints help).
```

```bash
python3 -m pl_transpiler.catalog --coverage
python3 -m pl_transpiler.catalog BarInterval
python3 -m pl_transpiler.catalog --search date
```

## Invariants

- **Dict-AST round-trip fixpoint.** For EL the parser accepts,
  `parse_el(emit_el(parse_el(el)))` equals `parse_el(el)` modulo line/column/warning
  metadata. `emit_el` derives EL **only** from the AST — it never reads the original
  source text.
- **Float32 semantics.** Library-function math and the parity comparison run at float32
  tolerance (`rtol = atol = 1e-6`); this tolerance is fixed and never loosened.
- **Fail-loud everywhere.** The forward transpiler, `emit_el`, `py_front`, and the
  runtime all **raise** on anything they cannot represent faithfully. No component emits
  or runs plausible-but-wrong output.
- **Canonical emission.** `emit_el` uses canonical keyword casing, explicit order syntax,
  infix `mod`/`crosses above|below`, and verbatim numeric literals; parentheses are
  re-derived from the parser's own precedence.
- **Strict default is all-or-nothing.** With `partial=False` the forward transpiler either
  produces the complete Python or writes nothing and raises `UnimplementedKeywordError`; it
  never emits a partial file in strict mode. This default is byte-identical to the
  pre-partial transpiler.
- **A partial stub ALWAYS raises at evaluation.** Under `partial=True`, an unimplemented
  construct is `pl_partial_stub(name, line)`, which raises `UnimplementedKeywordError` every
  time it is evaluated — never a default value, never a plausible-but-wrong result. A stub
  that is never reached simply never fires. Partial output is therefore **not faithful** to
  the source and exists only for incremental porting.

## Error semantics

- **User-input errors** (agent should fix the invocation): missing/incorrect CLI
  arguments → argparse exit code `2`; a keyword not found by the catalog CLI → exit code
  `1`; `FileNotFoundError` for a missing input path.
- **Unsupported-construct errors** (input is outside the supported surface): `PyFrontError`
  from `py_front`/`py_to_el`; a raised exception from `transpile`/`emit_el`/the runtime.
  These mean "this construct is out of scope," not "malformed call."
- **Fail-closed emission**: a nonzero exit from `el_emit` means the emitted EL did not
  pass `mc_ground_check` — do **not** treat its output as compile-safe.
- **Unimplemented keywords** (input uses EL this build has not ported):
  `UnimplementedKeywordError` with `.errors = [{'name','line'}, ...]`. Strict mode reports
  ALL of them at once (transpile time, `pl_transpile` exit `2`, no output file); partial mode
  defers each to a stub that raises at the evaluating bar. Read `.errors` to enumerate what to
  port; do **not** treat any partial-mode output as trustworthy.

## Test-tier map & skip semantics

| Tier | Command | Captures required | Skip behaviour |
|---|---|---|---|
| Public (portable) | `python3 tests/run_public_tier.py` | No | Every listed member ships; checks that need MultiCharts captures skip with a pointer to docs/CAPTURES.md; tier is GREEN without them |
| Full regression | `python3 tests/verify_all.py` (`--fast` quick pass) | Yes | Capture-gated sections skip with a pointer to docs/CAPTURES.md |
| Round-trip | `python3 tests/verify_roundtrip.py` | Yes | Skips cleanly when captures absent |
| Two-way | `python3 tests/verify_twoway.py` | Yes | Skips cleanly when captures absent |
| Full-range | `python3 tests/verify_full_range.py` | Yes | Skips cleanly when captures absent |

Capture-gated gates return success (skip) when their pinned input files are absent, so a
captures-less clone never reports a false failure. See
[docs/CAPTURES.md](../../docs/CAPTURES.md) to attach captures.

## Limitations (contract form)

- **Headline parity captures are excluded.** The per-bar MultiCharts ground-truth
  captures are licensed data and not redistributed; capture-gated tests **skip** (they do not fail).
- **This tree is the library, tests, and docs.** Internal build tooling is not shipped.
- **Validation scope = intraday index futures only** (1-minute @NQ; ES config registered, lighter validation). Other
  instruments/timeframes are **untested** — behaviour there is unverified.
- **Python input is a bounded mirror dialect**, not arbitrary Python: `py_front`/`py_to_el`
  accept only the clean Python the forward transpiler emits, and raise `PyFrontError`
  otherwise.
- **Fail-loud, not best-effort**: unsupported constructs raise; they are never guessed.
- **Partial-mode output is NOT FAITHFUL.** `--partial` / `partial=True` exists only for
  incremental porting: it replaces unimplemented constructs with stubs that raise on
  evaluation, so a stubbed strategy cannot produce a trustworthy backtest. Never treat
  partial output — or results computed from it — as representing the source strategy.

## Licensing

Licensed under the **Apache License, Version 2.0** (see [LICENSE](../../LICENSE)). The installed
package carries `License-Expression: Apache-2.0`. Attribution and the third-party carve-out
are in [NOTICE](../../NOTICE): the MultiCharts/TradeStation reference materials (under
`resources/`, `db/`, and any PDF keyword-reference document) are third-party copyrighted
content, **not** covered by the Apache grant. This public distribution does not include
them — they are excluded from the repository and from the installable wheel (the built
package contains none of them).

**Trademarks and affiliation.** PowerBridge is an independent open-source project. It
is not affiliated with, endorsed by, or sponsored by TradeStation Technologies, Inc. or
MultiCharts (MCT Limited). TradeStation and EasyLanguage are registered trademarks of
TradeStation Technologies, Inc.; MultiCharts is a registered trademark of MCT Limited;
PowerLanguage is a product name of MultiCharts. These names are used only to describe
compatibility and interoperability.
