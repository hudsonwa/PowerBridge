# Changelog

## Unreleased

### Added

- `docs/API.md`: a reference for the public library functions and the CLIs,
  with runnable examples. `pl_transpiler.__all__` lists the public names, and
  `run_oracle`, `load_bars`, `load_config_file`, `catalog` and
  `UnimplementedKeywordError` now import from `pl_transpiler` directly.
- The EL keyword catalog records how much evidence backs each keyword. Each line of
  `pl_signatures.jsonl` gains `kind` (`syntax` for language and filler words such as
  `If` or `Shares`, `builtin` otherwise), `arg_types`, `max_arity`, `verification` and
  `verified_by`, all generated from PowerBridge's own code and tests by
  `tools/gen_catalog_fields.py` (a test fails if the shipped file is stale and prints
  the regenerate command). `verification` is `n/a` for syntax words; for builtins it is
  `verified:capture-strategy` (used by a strategy whose per-bar outputs (required
  columns) match a MultiCharts capture in `tests/verify_all.py`; the captures are not public, so that
  check skips in a clone), `verified:test` or `documented-unverified`. `max_arity` and
  `arg_types` are filled only where PowerBridge itself defines an arity and are `null`
  otherwise. `catalog.kind()`, `catalog.arg_types()`, `catalog.max_arity()` and
  `catalog.verification()` read them, `catalog.coverage()` counts keywords per kind and
  level, and `pl_signatures.schema.json` describes a record. Existing fields are
  unchanged.
- The keyword catalog fills more gaps, each checked by running PowerBridge
  (`tests/test_catalog_returns.py` runs every keyword it fixes and compares the value
  type with `catalog.returns()`). The 16 color constants and `OpenEntriesCount` now
  return `numeric` instead of `unknown`, and 84 builtins that were on
  `mc_verified_builtins.txt` without a signature record (such as `CurrentBar`,
  `Symbol`, the `Array_*` helpers and the session-time words) gain one. Unknown returns
  drop from 184 to 171; the rest are declaration and type words, order statements,
  runtime stubs and the `Array_Set*Value` setters, whose return value PowerBridge's
  own code marks as unsettled, so they stay `unknown`. Three verified tokens still have no
  record, by design: `Abort` (a statement), and `CommandLine` and `EntryName` (not
  implemented). The test pins these counts.

## 0.6.0 — 2026-10-21

### Added

- `--max-entries-per-direction N` for `pl_run` and `pl_oracle`
  (`max_entries_per_direction=N` in `run_el` / `run_oracle`): a Buy or
  SellShort in the open position's direction is refused while N entries are
  open; reversals still fill. N=1 follows MultiCharts' documented default
  (not verified against a capture). The default stays unlimited, so existing
  digests do not change. The native kernel supports it; an older kernel build
  falls back to Python with a note.

### Changed

- **`transpile(src)` now returns the runnable dialect by default**, the same
  code `pl_transpile` writes and `pl_run` executes. Before, the library default
  was the clean mirror dialect, which keeps no cross-bar `_state`: EL Variables
  reset every bar, so a stateful strategy run from it silently produced zero or
  wrong orders. The mirror dialect (input for `el_emit`) now needs
  `transpile(src, dialect="mirror")`; `persist=False` still works. The bar loop
  refuses mirror-dialect strategies with a one-line error naming the fix.
  The `pl_transpile` CLI is unchanged (`--no-persist` still emits the mirror).
- Tests: the public tier is also a set of pytest tests marked `public_tier`, so
  `pytest` runs everything and `pytest -m public_tier` runs exactly the tier.
  `python tests/run_public_tier.py` still works (stdlib only) and shares the
  same member list.
- `run_el`, `load_bars_csv` and `get_config` can be imported from
  `pl_transpiler` directly; the old module paths keep working.
- The runtime finds its loud stubs from a generated static list
  (`tools/gen_loud_stubs.py`, checked by a test) instead of parsing its own
  source at import time, so it works from .pyc-only or zipped installs. The
  installed stubs are unchanged.
- README: `tools/build_accel.sh` needs an editable install (`pip install -e .`)
  for the console commands to see the built module.
- The public API (`transpile`, `run_el`, `run_oracle`, `load_bars_csv`,
  `load_bars`, `get_config`, `load_config_file` and the `catalog` accessors)
  has type annotations, and the package ships `py.typed`. CI runs mypy on
  those modules. Runtime behaviour and digests do not change.
- CI: every action in `ci.yml` and `accel.yml` is pinned to a commit SHA, the
  workflows run with a read-only token, Dependabot watches actions, cargo and
  pip, Python 3.14 joins the test matrix, and one pytest job uploads a
  coverage report.
- ruff also checks pycodestyle `E4`/`E7` and `W` rules.
- Dependabot runs monthly with one grouped PR per ecosystem (at most two
  open) and skips major versions, which are upgraded deliberately.

### Fixes

- A strategy that declares arrays but no Variables or Inputs no longer fails
  with `NameError: _state` in the trace and runnable dialects.

## 0.5.0 — 2026-10-21

### Added

- `docs/ARCHITECTURE.md`: the pipeline, the bar-loop/fill model, the f32/f64
  numeric model, the native kernel and how parity is established.
- PyPI metadata for `powerbridge` and `powerbridge-accel`: readme, author,
  classifiers, keywords and Documentation/Changelog/Issues links.
- Standard EasyLanguage shorthand: `O`, `H`, `L`, `C`, `V` and `I` (any case)
  read as `Open`, `High`, `Low`, `Close`, `Volume` and `OpenInt`, including
  `C[1]` and `Average(C, 10)`. A declared input or variable of the same name
  still wins, as documented for EasyLanguage. `Sell Short` and `Buy To Cover`
  are accepted as `SellShort` and `BuyToCover`; before, `Sell Short` failed
  with a misleading "unimplemented EL keyword 'short'". Shorthand and
  long-form strategies give identical digests (native and `--python`).
- `--max-bars-back N` for `pl_run` / `pl_oracle` (`max_bars_back=` on
  `run_el` / `run_oracle`, and in the native kernel): the first N bars are
  history only, with no strategy evaluation and no orders, and `BarNumber`
  starts at 1 on bar N+1. This is how MultiCharts runs a strategy (checked
  against a MultiCharts capture); pass `--max-bars-back 50` to reproduce its
  default. The default stays 0, so existing digests do not change. With too
  little history for a `Close[n]` read, one warning per run is printed on
  stderr (values unchanged). An accel build without MaxBarsBack support is
  refused for N > 0 and the run falls back to Python with a note.
- `--version` on `pl_transpile`, `pl_run`, `pl_oracle` and `el_emit`.
- The accel CI job runs the full pytest suite with the native kernel loaded.
- `--date-format dmy|mdy|ymd` on `pl_run`, `pl_oracle` and `bar_tape
  csv2pbar`, and `date_format=` on `load_bars_csv` / `load_bars` /
  `csv_to_pbar`. The default stays day-first (`dmy`); dates with a 4-digit
  first field (`2024-01-02`) are read as year-month-day.
- README: "Your own data" documents the bar CSV columns and date/time forms.

### Changed

- `run_el` no longer writes the "powerbridge-accel not installed" note to
  stderr; it logs it on the `pl_transpiler.tools.pl_run` logger. The `pl_run`
  and `pl_oracle` commands still print it, unchanged.
- Native-kernel skip messages and docs say `./tools/build_accel.sh` instead
  of `maturin develop --release`. Interface docs and comments describe what
  is stable and why, and `docs/dev/ACCEL-INTERFACE-V2.md` reads as a
  reference.
- `pl_run` CSV output rounds money columns (`NetProfit`, `GrossProfit`,
  `OpenPositionProfit`, ...) to cents and price columns (`EntryPrice`,
  `ExitPrice`, `AvgEntryPrice`, `Close`) to the instrument's tick decimals,
  so `1343.7984375000087` prints as `1343.80`. `--full-precision` restores the
  raw floats. `pl_oracle --fills-csv` rounds `price` the same way. Display
  only: `run_el` rows and every digest are unchanged.
- The `pl_oracle` summary line no longer ends in `N fast-path`. With
  `--verbose` it prints a second line saying how many bars had no pending
  orders, position or risk exits and skipped fill processing.
- The README examples write their files into `out/`, which git and
  `ruff check .` ignore, instead of the repository root.
- Install docs no longer suggest `pip install powerbridge[accel]`, which
  cannot resolve until `powerbridge-accel` is on PyPI: install the release
  wheel or build `accel/` from source. The README now lists the missing
  MaxBarsBack warm-up under Limitations.

### Fixes

- A lagged function call such as `Highest(High, Len)[1]`, the usual
  breakout idiom, no longer makes the Python path O(bars^2): it copied the
  whole price history on every bar. The lagged history is now kept per call
  site and extended in place. On a synthetic tape a breakout strategy took
  1.5 s / 4.1 s / 14.9 s for 20k / 40k / 80k bars and now takes
  0.5 s / 0.5 s / 1.0 s. Digests and all `ground_truth/` outputs are
  unchanged. This also fixes wrong values when two lagged recursive calls
  on different series, such as `XAverage(Low, 10)[1] + XAverage(High, 10)[1]`
  (or an RSI pair), shared an `id()`-keyed cache entry: the freed temporary
  slice of one could hand its id, and so its EMA/RSI state, to the other.
  The native kernel still refuses this construct and falls back to Python.
- `pl_run` and `pl_oracle` stop with exit code 2 and one `error:` line when
  a bars file has a header but no bars. Before, they exited 0 and printed a
  digest for an empty run.
- A call with the wrong number of arguments to a builtin whose arity is
  known (`x = Stochastic;`, `Stochastic(Close)`, a bare `MaxList`) now fails
  at transpile time with one line, e.g. `'Stochastic' at line 2 takes 11
  arguments, got 0`, from `pl_transpile`, `pl_run` and `pl_oracle`. Before,
  it transpiled and `pl_run` crashed with a `TypeError` traceback.
- `Ticks`, `UpTicks`, `DownTicks` and `OpenInt` no longer read as 0 when the
  bar data has no such column. In strict mode (the default) the run stops
  with one line naming the missing column; `--lenient` / `PL_STRICT=0` reads
  0 and lists it in the end-of-run warning. Data that supplies the column is
  used as before.
- `python -m pl_transpiler.catalog` no longer prints a runpy
  `RuntimeWarning`: the package `__init__` imports its public names lazily.
- The `FillEngine` docstring describes the stop/limit fills it implements,
  and code comments that narrated change history now describe behaviour.
- A native-kernel refusal in `pl_oracle` is reported once
  (`[pl_oracle] native path refused: ...; using the pure-Python engine
  (slower)`) instead of twice.
- The default runnable `pl_transpile` output now runs in time linear in bar
  count. Lookback variables passed to series functions copied their whole
  history every bar, so the README's `run.py` loop was quadratic (40k bars:
  about 6.7 s, now 0.3 s). The output seeds `kwargs['_state']` with the same
  O(1) history views `pl_run` uses; signals are unchanged, and trace and
  `--no-persist` output are byte-identical.
- `pl_oracle` with the native kernel installed no longer prints the
  "falling back to the Python path" note before a one-line user error
  (unimplemented keyword, undeclared variable, strict-mode stop) (#80). The
  note is printed once the Python run completes.
- `el_emit` on Python outside the mirror dialect, or on input that is not
  valid Python, prints `el_emit: error: ... at line N` and exits 2 instead of
  a traceback with exit 1 (which read like a failed name check).
- `pl_run --columns` suggests and lists EasyLanguage names
  (`MarketPosition`, `CurrentBar`, ...) instead of internal trace keys such as
  `_market_position`.
- README, `llms.txt` and `docs/LLM_DIALECT.md` agree on the validation scope
  (@NQ and @ES) and the current residual gaps.
- Bar CSV dates and times are validated: a month outside 1-12, a day that
  does not exist, a time outside 00:00-23:59 or a time with non-zero seconds
  now stops `pl_run` / `pl_oracle` with one error line naming the file, line
  and value (exit 2). Before, `01/13/2024` loaded as month 13 and seconds
  were dropped silently.
- A day-first date whose month field is above 12 (a month-first export) gets
  a hint to pass `--date-format mdy`.

## 0.4.0 — 2026-10-20

### Fixes

- **Trade digest v2: the fill log now records every position change.**
  `FillEngine.fills` (hashed by `pl_oracle`, written by `--fills-csv` /
  `--dump`, returned by `run_el` as `trades`) logs a reversal as its close leg
  followed by its open leg (before, long 1 -> short 1 logged nothing), and
  logs `SetStopLoss` / `SetProfitTarget` / other Set* risk exits and
  SetExitOnClose. `round_turns` counts exit records instead of `fills // 2`.
  The native kernel records the same fills. Position, PnL and the per-bar
  trace are unchanged. The digest header is now `powerbridge-trade-digest v2`
  and the hashed text starts with `digest_version: 2`, so every digest
  changes; `pl_oracle --digest-v1` prints the old bytes for pinned v1
  digests. See docs/verification.md.
- The native kernel exports `FILL_LOG_VERSION` and the core checks it before
  using the kernel (#78). A kernel with a different fill log, or one that
  predates the check (the 0.3.0 wheel), is not used: the core prints
  `powerbridge-accel X implements fill-log v1; this powerbridge needs
  fill-log v2; using the Python path` and runs the Python engine, so the
  native path can no longer return different fills.
- `el_emit` installed from the wheel no longer skips its name check (#76).
  The proven tokens and call forms from the `ground_truth/` scripts ship as
  `pl_transpiler/tools/mc_proven.json` (regenerated by
  `scripts/gen_mc_proven.py`; a test keeps it in sync), so the installed
  command runs the same name check as a source checkout and passes the
  README's GT2 example instead of a false `sellshort: too few args`. If the
  data is missing, `el_emit` exits 2 with a one-line error rather than
  passing. A CI job checks this from a fresh venv.
- Python -> EL (`el_emit`, `py_to_el`) writes the canonical MultiCharts name
  when several EL spellings map to one builtin (#81): `AvgTrueRange` instead
  of `atr`, `RateOfChange` instead of `roc`, `StandardDev` instead of
  `stddev` and `WAverage` instead of `weightedaverage`. GT1 (which uses
  `AvgTrueRange`, `RateOfChange` and `StandardDev`) now round-trips EL ->
  Python -> EL and passes the name check. Forward transpile output is
  unchanged.
- `pl_run` and `pl_oracle` report an unimplemented EL keyword (for example
  `TradesToday`) as one `<tool>: error: ...` line naming the keyword and
  source line, exit 2, instead of a Python traceback.
- Assigning to a variable that was never declared (`MyTotal = 3;` with no
  `Vars: MyTotal`) is reported as `undeclared variable 'MyTotal' at line N
  (declare it under Vars:)` instead of `unimplemented EL keyword 'mytotal'`.
- Fixed dead links in `ground_truth/captures/MANIFEST.md`.

### Added

- `--config` on `pl_run` and `pl_oracle` also accepts a path to a JSON
  instrument config with exactly the `InstrumentConfig` fields (#74). Unknown
  or missing keys and wrong types are a one-line error, exit 2. The built-in
  NQ and ES configs ship as `examples/configs/NQ.json` and `ES.json`; the
  README's "Other instruments" section documents the format and the engine's
  commission, slippage and position-limit assumptions.
- The README examples run in CI (`tests/test_readme_examples.py`) and their
  documented output is compared; blocks that install packages, need the
  network or a Rust toolchain are marked `<!-- readme-test: skip -->`.

### Changed

- 0.4.0 is released on GitHub only: install the wheels attached to the
  v0.4.0 release (see the README). The release workflow publishes to PyPI
  only when the repository variable `PYPI_PUBLISH` is `true`.
- The `accel` extra pins `powerbridge-accel==0.4.0`.
- README corrections found by the example test: the library example prints
  7 fills, not 4, and the `el_emit` example uses GT2.
- Re-measured the native speed-up with `tools/bench_native.py`: about 5x at
  20,000 and 100,000 bars (was "20x / 100x"); the Python path scales linearly.
- Limitations lists #47 (`Sell from entry("A")` closes the whole position;
  documented behaviour, not yet verified against MultiCharts).

## 0.3.0 — 2026-10-20

### Changed

- **Strict mode is now the default.** A run that calls an EasyLanguage builtin
  the runtime does not implement stops with a one-line error (the builtin's
  name, its first source line and how to opt out) instead of silently using
  0 or ''. To get the old behaviour, pass `--lenient` to `pl_run` / `pl_oracle`
  or set `PL_STRICT=0`. Cosmetic builtins (drawing, sound, print) never stop a
  run. `PL_STRICT=0` / `false` / `no` / `off` now also turn off the per-bar
  re-raise in the bar loop instead of enabling it. (#31)
- `pl_transpile`, `pl_run`, `pl_oracle` and `el_emit` print
  `<tool>: error: <message>` and exit 2 for common mistakes (EL syntax error,
  missing input file, unknown `--config`, malformed bars CSV) instead of a
  traceback. `PB_DEBUG=1` shows the full traceback. `-o -` writes to stdout.
  `[accel] skip-region` diagnostics print only with `pl_oracle --verbose` or
  `PB_DEBUG=1`. (#58)
- `pl_run --columns` rejects unknown column names with close-match suggestions
  (it used to print 0 on every bar) and accepts EasyLanguage spellings such as
  `MarketPosition` and `NetProfit`. (#56)
- The pure-Python bar loop is now linear in bar count: 128,000 bars of the GT2
  sample take about 3 s instead of 57 s. Emitted Python and results are
  unchanged. (#51)
- Internal refactors with byte-identical output: `runtime/pl_runtime.py`,
  `codegen.py`, `parser.py` and `engine.py` are split into smaller modules.
  (#19, #21, #22, #23)
- Plain-language check messages, a simpler first-run Quickstart, and cleaned-up
  docs, comments and help text. (#18, #24, #30, #48, #50)

### Added

- Release workflow: publishing a GitHub Release attaches the sdist, the pure
  wheel and prebuilt abi3 `powerbridge-accel` wheels (macOS arm64/x86_64,
  Windows x64, Linux x86_64/aarch64), then publishes `powerbridge` and
  `powerbridge-accel` to PyPI through trusted publishing. (#52)
- Builtins: `SetDollarTrailing` and `SetTrailingStop_pt` in both engines (#46);
  `TickID` and `ExecOffset` (0 on every historical bar, as in a MultiCharts
  backtest) (#44); `Symbol`, `SymbolName`, `Description`, `Category`,
  `SymbolCurrencyCode` and `ExpirationDate` (#39); `MaxShares`,
  `MaxSharesHeld`, `OpenEntriesCount`, `MillisecondsFromDateTime` and
  `SessionStartDay` (#38); `Array_Compare`, `Array_Copy`, `Array_SetValRange`
  and `Array_Sort` (#37); `Fill_Array` and the point-based `Set*_pt` stops
  (#35); the computer-clock builtins `CurrentTime_s`, `ComputerDateTime` and
  `LastCalc*` (#33).
- `pl_oracle --fills-csv FILE` writes a readable fill log, and
  `pl_oracle.fill_trade_types` derives the position-aware trade type
  (EntryLong / ExitLong / EntryShort / ExitShort). The hashed fill field is
  unchanged and now documented as the buy/sell side. (#45)
- `tools/bench_native.py`, a reproducible Python-vs-Rust timing with a
  fill-identity check, and a CI matrix on Linux, macOS and Windows with
  Python 3.9 and 3.13. (#12)
- CI runs the full pytest suite and ruff. (#16)

### Fixes

- `EntryDate(n)`, `EntryTime(n)`, `EntryPrice(n)`, `ExitDate(n)`,
  `ExitTime(n)` and `ExitPrice(n)` now return values for past positions
  instead of 0 or the current position. (#26)
- Array builtins (`Array_Get*Value` / `Array_Set*Value`, `Array_IndexOf`,
  `Array_Sum`, `Array_Highest`, `Array_Lowest`, `SortArray`, `VarianceArray`,
  `Array_SetMaxIndex`) now address the same elements as direct `A[i]` access.
  (#43)
- The Rust accelerator loads deeply nested programs (such as long else-if
  chains) instead of failing with a recursion-limit error. (#28)
- First-run problems: the missing `examples/ma_cross.el` demo file and a
  Python 3.13 failure in the f32 test oracle. (#12)
- Test suite: the generated tests and `test_failloud.py` run by default and
  pass, the public tier runs on shipped files only with actionable skips, and
  `ground_truth/captures/` is ignored again. (#13, #14, #42, #55)

## 0.2.0 — 2026-10-20

- Distribution renamed `pl-transpiler` -> `powerbridge`; the Rust wheel is `powerbridge-accel`.
  The import name stays `pl_transpiler`.
- Optional Rust fast path (`accel/`, module `pl_transpiler._accel`): native bar
  loop and fill engine in one FFI call, Data2 support, batched and threaded
  input sweeps, prefix-skip and slice-before-marshal, typed trace bridge,
  in-process audit packets, `.pbar` binary tape sidecar.
- `pl_oracle` command: exact trade-list digest for parity gates.
- `ES` (@ES#C RTH 1-min) instrument config.
- Input overrides apply case-insensitively.
- `--from-date` / `--from-bar` windows with seeded warm-up on `pl_run` and `pl_oracle`.
- `persist=` / series-layout contract; arrays accepted by persist/trace.
- Validated against MultiCharts on 1-minute @ES (4 configurations, 4,278 fills identical).
- `pl_oracle` uses the Rust kernel by default when installed (`--python` forces Python);
  any Python-path run prints why. An installed accel module is found from inside a checkout.
- Unimplemented builtins used by a run are reported loudly (`PL_STRICT=1` still raises).
- Fixed: a strategy Input named like a reserved word (e.g. `ExitTime`) was read as the builtin.
- Fixed: `Input[n]` bar references crashed.
- Fixed: Inputs named `Len` / `Prec` were overwritten with 14 / 6 (Python and Rust).
- Fixed: `FormatDate`/`FormatTime` crashed on `ELDateToDateTime` values.
- Fixed: Rust kernel sized a quantity-less entry from the open position (fills doubled every bar).
- Fixed: tests and tools hard-coded `/usr/bin/python3` and `/usr/bin/perl` (Windows).

## 0.1.0 — 2026-07-15

- First public export: EasyLanguage ⇄ Python transpiler, `el_emit`, `pl_run`,
  `pl_transpile`, keyword catalog, fail-loud unsupported-keyword reporting.
