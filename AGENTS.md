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

# AGENTS.md — PowerBridge machine-facing contract

This file documents PowerBridge for machine consumers (agents, scripts, other tools)
per the [AGENTS.md](https://agentsmd.net) convention. It is a precise contract, not a
tutorial; the human-facing guide is [README.md](README.md).

All examples run from the repository root and assume `pip install .` has been done.
Two shipping fixtures are referenced throughout:
`ground_truth/GT1_functions_indicators.txt` (an EL indicator) and
`examples/NQ_sample_bars.csv` (synthetic OHLCV bars).

## Package layout

- `pl_transpiler` — the transpiler package (pure standard library, no third-party deps).
  - `transpile` — forward transpiler (EL → Python).
  - `pl_transpiler.reverse` — reverse-transpiler surface (EL/AST/Python → EL).
  - `pl_transpiler.py_front` — the mirror-dialect Python front-end (Python → dict AST).
  - `pl_transpiler.catalog` — query API over the distilled EL keyword catalog.
  - `pl_transpiler.tools.el_emit` — reverse-transpiler CLI (`el_emit`).
  - `pl_transpiler.tools.pl_run` — run-EL-over-bars CLI (`pl_run`).
  - `pl_transpiler.runtime.*` — the per-bar execution engine, instrument config, runtime.
- `tests/` — the verification gates (shipped, not packaged into the wheel).
- `ground_truth/`, `emitted/`, `examples/` — EL sources and sample data.

The shared **intermediate representation (IR)** is the parser's **dict AST**: a plain
`dict` with a `type`, a `body`, and retained metadata. Every public conversion is a
composition over this IR.

## Python API contracts

### `pl_transpiler.transpile(pl_source: str, trace: bool = False, partial: bool = False) -> str`

Convert PowerLanguage/EasyLanguage source to Python source.

- `pl_source`: EL source text.
- `trace`: when `True`, the generated Python records every per-bar variable value into
  a `_trace` dict (used by the runtime/`pl_run`); when `False`, emits the clean
  **mirror-dialect** Python that `py_front` can read back.
- `partial`: opt-in FL2 partial mode.
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
print("transpile OK:", len(py), "chars")
```

### `pl_transpiler.errors` — collect-all transpile diagnostics (FL1/FL2)

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

run_el(el_source: str, bars: list[dict], config, *, trace_columns: list[str] | None = None,
       partial: bool = False)
    -> {'columns': list[str], 'rows': list[list[str]], 'trades': list}
    # Transpile (trace=True) and run EL bar-by-bar over supplied bars + instrument config.
    # trace_columns=None -> the first bar's sorted trace keys become the columns.
    # partial=False (default): unimplemented keywords raise at TRANSPILE time, before any
    #   bar runs — byte-identical to before.
    # partial=True: transpiles in partial mode and prints the stub manifest loudly to
    #   stderr first; a stubbed construct RAISES UnimplementedKeywordError at the bar it is
    #   evaluated on (message "partial-mode stub executed ..."). A stub never reached (e.g.
    #   behind a false guard) never fires, and those rows match the strict run with that
    #   line removed. The CLI exposes this as `pl_run --partial`.
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
print("pl_run library OK:", len(out["rows"]), "rows,", len(out["trades"]), "trades")
```

## CLI contracts

### `pl_transpile` (`pl_transpiler.tools.pl_transpile`)

```text
pl_transpile <in_file> [-o OUT] [--trace] [--partial]
python3 -m pl_transpiler.tools.pl_transpile <in_file> [-o OUT] [--trace] [--partial]

  in_file    : EL/PowerLanguage source file.
  -o OUT     : write Python here (default: stdout).
  --trace    : emit trace-mode Python (per-bar variable capture) instead of the clean dialect.
  --partial  : opt-in partial mode (see below).

  DEFAULT (strict): valid source -> exit 0, Python written. Unimplemented keywords ->
    the FL1 complete report (EVERY keyword, name + line) to stderr, NO output file, exit 2.
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
python3 -m pl_transpiler.tools.pl_run <el_file> <bars_csv> [--config NAME] [-o OUT] [--columns a,b,c] [--partial]

  el_file    : EL/PowerLanguage source file.
  bars_csv   : OHLCV CSV (date,time,open,high,low,close,volume). REQUIRED.
  --config   : instrument config name (default: NQ).
  -o OUT     : write the predicted per-bar CSV to OUT (default: stdout).
  --columns  : comma-separated trace columns to emit (default: all trace keys).
  --partial  : opt-in partial mode (run_el partial=True): unimplemented keywords become
               execution-time stubs, the stub manifest is printed loudly to stderr, and a
               stub RAISES UnimplementedKeywordError the moment a bar evaluates it. A stub
               never reached does not fire. Default (no flag): unimplemented keywords raise
               at transpile time before any bar runs. NOT FAITHFUL — do not trust its output.

  stdout : the per-bar CSV (unless -o given).
  stderr : a one-line "[pl_run] N bars, M rows, K trades" status line.
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
| Public (portable) | `python3 tests/run_public_tier.py` | No | Capture/private members skip with a one-line note; tier is GREEN without them |
| Full regression | `python3 tests/verify_all.py` (`--fast` quick pass) | Yes | Capture-gated sections skip with a pointer to docs/CAPTURES.md |
| Round-trip | `python3 tests/verify_roundtrip.py` | Yes | Skips cleanly when captures absent |
| Two-way | `python3 tests/verify_twoway.py` | Yes | Skips cleanly when captures absent |
| Full-range | `python3 tests/verify_full_range.py` | Yes | Skips cleanly when captures absent |

Capture-gated gates return success (skip) when their pinned input files are absent, so a
captures-less clone never reports a false failure. See
[docs/CAPTURES.md](docs/CAPTURES.md) to attach captures.

## Limitations (contract form)

- **Headline parity captures are excluded.** The per-bar MultiCharts ground-truth
  captures are private and not shipped; capture-gated tests **skip** (they do not fail).
- **A private production-strategy parity suite is excluded** by design; nothing shipped
  depends on it.
- **The development/orchestration machinery is not part of this export.**
- **Validation scope = intraday index futures only** (1-minute @NQ and @ES). Other
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

Licensed under the **Apache License, Version 2.0** (see [LICENSE](LICENSE)). The installed
package carries `License-Expression: Apache-2.0`. Attribution and the third-party carve-out
are in [NOTICE](NOTICE): the MultiCharts/TradeStation reference materials (under
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
