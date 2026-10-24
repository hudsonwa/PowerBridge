# Changelog

## About the early releases

Versions 0.1 to 0.9 were the first public releases of a tool that had been
developed privately. They were cut over a few days, one as each batch of
verified features landed. From here, releases are spaced out and made when
there is enough new content to justify one.

## Versioning and stability

PowerBridge follows [Semantic Versioning](https://semver.org/). Before 1.0:

- A **patch** release (0.x.Y) only fixes bugs; existing strategies give the same
  fills and the same digests.
- A **minor** release (0.X.0) may add features. It may also change behaviour,
  but only with a note under "Changed" saying what changed and why.
- The stable surface is the `pl_*` command-line flags, the names documented in
  [docs/API.md](docs/API.md) and the `pl_oracle` digest format. Anything else
  (modules not listed there, `_accel` internals) can change without notice.
- Something on the stable surface is removed only after one minor release in
  which it still works and emits a `DeprecationWarning` (or a CLI warning).

1.0 will freeze that surface under the usual SemVer rules.

## Unreleased

### Added

- Each GitHub Release now carries a `SHA256SUMS` file over every sdist and wheel and a build provenance attestation for each file, so a download can be checked with `sha256sum -c` and `gh attestation verify`; see SECURITY.md (#251).
- `Print` output is shown: `pl_run` and `pl_oracle` write a strategy's `Print` lines to stderr, or to a file with the new `--print FILE`, and `run_el` / `run_oracle` return them as `prints`. The native kernel prints the same lines as the Python engine. Fills, digests and stdout are unchanged. Generated Python calls `pl_print(...)` instead of `print(...)` (#254).

- `pl_examples` also writes `examples/configs/ES.json`, `examples/configs/NQ.json` and `examples/hand_written.py`, so the USAGE.md and PYTHON_STRATEGIES.md commands work after a wheel install; CI now runs the documented commands from an empty directory against the built wheel, and the docs mark unreleased features *new in the next release (#N)*, checked against this section (#258).

### Changed

- When the native kernel is installed but cannot run a program, the note on stderr is now one line saying the Python engine ran and why (`[pl_oracle] note: ran on the Python engine because the native kernel does not support bar_ref series yet; ...`); `-v` adds the kernel's own refusal text (#254).
- The release workflow now tests the built wheels before anything is attached or published: the `powerbridge` wheel runs the public tier from a clean venv, and each `powerbridge-accel` wheel runs a native-vs-Python digest check on the examples on its own platform runner (#246).
- `run_el` no longer takes the private `_python_path_notify` keyword; `pl_run` uses an internal helper instead (#242).
- The maintainer-only capture reader `analyze_capture.py` moved from `pl_transpiler/tools/` to the repository's `tools/`, so the wheel no longer ships it; the unused `tools/measure_audit_packet.py` is gone, and `ground_truth/captures/MANIFEST.md` is now a short data card (#248).
- Internal: strict mode and the unimplemented-call counts are kept per run, and the EL emitter's comment maps per call, so two `run_el` / `run_oracle` runs with different strictness no longer interfere. (Emits in threads could still mix up comments until #256.) The engine's trade-stat trace hook is `FillEngine(capture_stats=...)` / `set_capture_stats()` instead of a fixture-named private flag. Output and digests are unchanged (#244).

### Fixed

- Mirror-dialect output (`pl_transpile --dialect mirror`) no longer reads `_state` when a Variable is passed to `CrossesAbove` / `CrossesBelow` or another series function; it passes the bare variable, as the docs say. `el_emit` gives the same EasyLanguage as before, and runnable output is unchanged (#242).
- `pl_run` / `pl_oracle --help` no longer point at `examples/configs/ES.json` or `csv_to_pbar`, which a wheel install does not have (#242).
- docs/API.md shows the full `run_el` / `run_oracle` signatures (`drawings`, `drawing_case_id`, `drawing_tape`, `use_native`), says why `seed_warmup` defaults to `True` in `run_el` and `False` in `run_oracle`, and a test now fails when a documented signature drifts from the code. USAGE.md and PYTHON_STRATEGIES.md use `--dialect mirror` and drop the redundant `--max-bars-back auto` (#242).
- LIMITATIONS.md and USAGE.md no longer call MaxBarsBack opt-in or say the default stays 0: they describe `auto` as the default and `0` as the opt-out, and the USAGE.md section is shorter. ACCEL.md no longer lists `Print` as refused by the native kernel. A test now fails if those stale phrases return (#250).
- `emit_el`, `parse` and `transpile` called from several threads at once no longer mix up comments between sources: the lexer returns comments with its tokens (`tokenise_with_comments`) instead of through the module-level `lexer.last_comments` list, which is gone, and the parser's shared lex/parse cache is locked. Output is unchanged (#256).

## 0.11.0 — 2026-10-24

Highlights:

- `--max-bars-back` defaults to `auto`, so a strategy's warm-up bars no longer trade where MultiCharts' would not, and the quickstart needs no flag (#237).
- Bars CSVs are checked: the slash-date order is inferred (or the run asks for `--date-format`), and bars that go back in time stop the run (#229, #237).
- EL run-time errors and a missing `;` stop with one line naming the EL line, instead of a traceback or a silent run (#223, #235).
- The `run_el` / `run_oracle` library lives in `pl_transpiler.run` and `pl_transpiler.oracle`, and `pl_transpile --dialect` names the output dialect (#217, #227).
- **Some runs give different results or now stop**; see the behaviour changes under Changed.

### Added

- `pl_transpile --dialect {runnable,mirror}` selects the output dialect with the same names as `transpile(src, dialect=...)`; `--no-persist` stays as an alias for `--dialect mirror` (#217).
- `InstrumentConfig.round_turn_cost` ($ per contract, default 25.0) sets the cost the native audit packet subtracts from each closed trade, instead of a fixed $25. The built-in NQ and ES configs keep 25.0, so their packets are byte-identical; fills and digests do not depend on it (#233).

### Changed

- **Behaviour change:** `--max-bars-back` on `pl_run` and `pl_oracle` defaults to `auto`, and `max_bars_back` in `run_el` / `run_oracle` to `None` (auto), as MultiCharts auto-detects MaxBarsBack. When the lookback cannot be inferred, the default falls back to 0 with a stderr note (an explicit `auto` still stops with an error). The status line shows the number used (`max-bars-back 21 (auto)`). Pass `--max-bars-back 0` (`max_bars_back=0`) for the old results; digests with an explicit value are unchanged on both engines (#237).
- **Behaviour change:** without `--date-format` (`date_format=None` in `load_bars_csv` / `load_bars`), the slash-date order of a bars CSV is inferred from the whole file. When it cannot tell, or both orders occur, the run stops with one line asking for `--date-format mdy` or `dmy` (exit 2). Before, slash dates were read day-first, so a month-first file holding only days 1-12 was silently misread. EL integer dates load as before (#237).
- **Behaviour change:** `pl_run`, `pl_oracle`, `load_bars_csv` and `load_bars` stop on bars that go back in time, naming the CSV line (`bars out of order at line 4: 2024-01-02 09:29 comes after 2024-01-02 09:31`), with exit 2 (`ValueError` in the library). Bars with the same date and time are still accepted (#229).
- **Behaviour change:** a statement or declaration without its closing `;` is a syntax error (`missing ';' at the end of the statement`, line, caret, exit 2). The documented omission before `Else` and attributes like `[IntrabarOrderGeneration = False]` still parse, and `Close Data2`-style forms once reported as unimplemented keywords now stop at the missing `;`. Generated code is unchanged for every file that still parses (#235).
- A run-time error in the EL program (array index out of range, division or `Mod` by zero, any other exception from the generated code) stops `pl_run` and `pl_oracle` with one line naming the EL line and exit 2, instead of a traceback and exit 1. The library raises the new `ELRuntimeError` (with `.line`), which is also an instance of the original exception type; `PB_DEBUG=1` shows the traceback (#223).
- The library code behind `pl_run` and `pl_oracle` moved to `pl_transpiler.run` and `pl_transpiler.oracle`; the CLI modules are thin argument parsers and the old import paths still work (#227).
- The `--help` text of every command is in plain language, with maintainer options listed last under "advanced options"; option names and behaviour are unchanged (#225, #229).
- `pl_transpiler.engine` no longer re-exports private helpers (`_parse_el_date`, `NQ`, ...); import them from `engine_dates` and `runtime.instrument_config` (#217).
- The `AUDIT_REF_3_*` pinned-digest constants moved to `tests/reference_pins.py`, since only the tests use them (#233).
- Internal: the native kernel has one Rust fill engine; the unused copy in `accel/src/engine/` is gone. Fills and digests are identical (#231).
- The dev tools are pinned in the `dev` extra and CI installs it, so `pip install -e '.[dev]'` gives the same lint, type-check and test results as CI (#221). The core modules are fully type-annotated and mypy runs with `check_untyped_defs` (#215).

## 0.10.0 — 2026-10-24

Highlights:

- `pl_oracle --summary` prints a performance summary of the closed trades.
- `--version` says whether the native kernel is available, and why not.
- A plain `pytest` run without the native kernel is short and quiet, and `tools/dev_setup.sh` sets up a full development environment (#186, #188).
- The lexer runs in linear time and `pl_run` / `pl_oracle` parse a program once per run (#180, #182).

### Added

- `pl_oracle --summary` prints a performance summary of the closed trades to stderr: net, gross profit and loss, trades, winners, losers, win rate, closed-trade max drawdown and open contracts. P&L uses the big point value, before commission and slippage. The text is the same on both paths and the digest is unchanged.
- `--version` on the command-line tools says whether the native kernel is available and, if not, why; `native_status()` gives the same check from Python (#173).
- `tools/dev_setup.sh` sets up a development environment in the active venv: upgrades pip, installs `.[dev]` editable, builds the native kernel when `cargo` is available and prints the kernel status. CONTRIBUTING leads with it and CI runs it in a fresh venv (#188).

### Changed

- `Sell` / `BuyToCover ... from entry("X")` stops (exit 2) only when an entry with a different name is open on its side; with entry X alone open, even pyramided, it closes the position as before 0.9.0 (#24).
- Without the native kernel, a plain `pytest` run leaves the kernel-only tests out and prints one line with their count and how to build the kernel, instead of about 600 skips; `-m needs_native` still collects them (#207). The remaining skips are grouped by category with a pointer to the setup docs (#186).
- The lexer runs in linear time (a 3,200-line program lexes in about 0.04 s instead of 2 s), and `pl_run` / `pl_oracle` lex and parse a program once per run instead of up to ten times; output is identical (#180, #182).
- Packaging is declared in `pyproject.toml` only; `setup.py` is gone and the `pl_examples` files live in `pl_transpiler/_examples/`. The wheel contents are unchanged (#192).
- `pl_transpiler/builtins.py` imports its runtime functions by name, so mypy and ruff check the builtin table, and mypy runs with stricter flags (#198, #200).

### Fixed

- Contributor setup works on Python 3.9's bundled pip: CONTRIBUTING upgrades pip before the editable install, and CI runs that setup on 3.9 (#175).
- A strict-mode stop (`from entry ("X")` with another named entry open, a missing bar column such as `OpenInt`, an unimplemented builtin) names the line of the statement that raised it, not the first line mentioning the word (#178).
- With several front-end errors, each unknown keyword is labelled `unimplemented EL keyword 'foo' at line N`, as for a single error (#196).

## 0.9.0 — 2026-10-23

Highlights:

- `--max-bars-back auto` works out the strategy's lookback for you (#163).
- Series functions such as `Average` take arithmetic, offset and nested-call price arguments and give the correct result (#165, #167).
- `crosses above` / `crosses below` compare the right bars when MaxBarsBack skips bars (#161).
- Several programs that the native kernel ran differently from Python now fall back to Python (#153, #155).
- **Some programs give different, correct results**; see the behaviour changes under Fixed (#159, #161, #167).

### Fixed

- **Behaviour change.** A series function whose price argument is arithmetic or a nested call now uses that argument's own per-bar history; before, `Average(Value1 * 2, 5)` and `Average(Value1 + Value1, 5)` silently gave wrong values and `Average(Average(Close - 1, 3), 5)` raised `TypeError`. Results change for price arguments that add two series or variables (`High + Low`, `Close + Value1`) or multiply one by a whole number (`Close * 2`, `2 * MyVar`); plain arguments are unchanged, and an argument that reads a variable inside an `If`, loop, `Once`, `Switch`, `IFF` or `and` / `or` stops with an error (exit 2) (#167).
- Indicator calls that crashed at run time now run or stop with a one-line error (exit 2): `Average((High + Low) / 2, 10)`, `Highest(Close[5], 10)`, `Average(Range, 5)`, `Range[2]`, `TrueRange[2]` and `Variance(Price, Length, DataType)` run on the Python path. A negative TgtBar in `LinearRegValue` stops with an error; programs that ran before are unchanged (#165).
- **Behaviour change.** `crosses above` / `crosses below` between a variable and a price series or function result (`AvgC crosses Close`) compared the wrong bars under MaxBarsBack above 0 on both paths and now gives a different, correct result. It no longer raises `IndexError` on the Python path; crosses between two series, two variables or two functions, and all runs with MaxBarsBack 0, are unchanged (#161).
- **Behaviour change (Python path).** A variable declared with a negative initial value (`Vars: TLID(-1);`) is now numeric, so drawing ids compared or passed to getters no longer raise `TypeError`. Programs that compared such a variable to its initial value or relied on its full double precision now give a different result, matching the native kernel; `Date[n]` / `Time[n]` before the start of the tape read 0 instead of raising (#159).
- A drawing anchor that is not a bar on the tape (e.g. `TL_New(Date[1], Time[1], ...)` on bar 1) no longer fails; both paths record it with a null `bn` and the drawing dumps match. `--explain-native` now says it is a compile-time check (#157).
- The native kernel runs `EntryName(n)` / `ExitName(n)` and the bracket form of an input (`Len[1]`), so inputs such as `ExitTime(1555)` no longer refuse with "bar_ref series" (#153).
- The native kernel refuses a program that reads an input named like a per-bar runtime key (`Inputs: MaxShares(2)`) and falls back to Python, which gave different trades before; `--explain-native` names the collision (#153).
- The native kernel refuses a program that reads an `OutPath` input outside a file path and falls back to Python, which gave different results before; `Print(File(OutPath), ...)` still runs natively (#155).

### Added

- `--max-bars-back auto` on `pl_oracle` and `pl_run` (and `max_bars_back="auto"` in `run_oracle` / `run_el`) uses the lookback inferred from the source, shows it on the status line (`max-bars-back 11 (auto)`) and stops with an error naming the construct when it cannot be inferred. Without the flag results are unchanged; only the stderr warm-up note's wording changes (#163).

## 0.8.1 — 2026-10-23

### Added

- `pl_examples` writes the quickstart's example strategy and synthetic bars into `./examples/` (never overwriting), so the README quickstart works straight after a wheel install (#147).
- The native kernel runs the positions-ago trade history `EntryDate(n)`, `EntryTime(n)`, `EntryPrice(n)`, `ExitDate(n)`, `ExitTime(n)`, `ExitPrice(n)` and `MaxShares(n)`, in call and bracket form, plus their bare forms (#149).
- The native kernel runs `Floor`, `ELDateToDateTime` and the computer-clock words `CurrentTime_s`, `CurrentDate_s` and `ComputerDateTime` (#145).
- The native kernel runs `StrLen`, `FormatDate`, `NewLine`, `Text_SetColor` and the `FileAppend` / `FileDelete` no-op stubs (#138).
- A `[dev]` extra (`pip install -e '.[dev]'`) installs the test, lint and wheel-build tools (#140).

### Changed

- `pl_run` writes its per-bar rows with the Rust kernel when it is installed and the run is full-tape (byte-identical CSV; `--native` / `--python` as in `pl_oracle`, and `[native]` / `[python]` on the status line) (#3).
- The native kernel refuses a string mixed with a number instead of converting it (#138).
- The README quickstart starts from the wheel install, explains the stderr status line and adds a tested "Your own strategy" section (#136, #140, #151).
- LIMITATIONS notes that `el_emit` does not check the argument types of multi-argument functions (#7).
- CONTRIBUTING has a generated table of default test skips (`tools/skip_report.py`) and says what public CI covers (#136).

### Fixed

- `--explain-native` names a refused keyword by its EasyLanguage spelling (`MaxPositionProfit`) instead of the internal name (#149).
- `pl_run`, `tools/gt_sweep.py` and `tools/accel_gate.py` count round turns from exit records, like `pl_oracle`, instead of `fills // 2` (#140).

## 0.8.0 — 2026-10-23

### Added

- The native kernel runs variables declared inside a block (`Vars:` inside `begin ... end`), so these programs no longer fall back to Python (#128).
- `--explain-native` lists the Python 3.12+ refusal of an `Average` over a series of unknown int/float type (#128).
- The native kernel runs the `total` order modifier and `from entry ("X")` named exits with a literal label (#125).
- The native kernel runs `Round(value, precision)`, `MinMove`, `PriceScale` and `LastBarOnChart_s` (#122).
- The native kernel runs `CurrentSession`, `SessionCount`, `SessionCountMS`, `SessionEndDay` and `SessionEndDayMS` (#113).
- The native kernel runs `SessionEndTime`, `SessionStartTime` and the sibling session-time words (#109).
- `pl_oracle --explain-native FILE` (and `explain_native()`) lists every construct the native kernel would refuse (#111).
- A pytest run that skips tests ends with a summary of skips grouped by reason (#106).
- `docs/USAGE.md` has a "Warm-up and MaxBarsBack" section (#106).
- `docs/PYTHON_STRATEGIES.md` teaches the mirror dialect from scratch, with a tested example in `examples/hand_written.py` (#102).
- `pl_run` and `pl_oracle` print a warm-up `note:` when a strategy reads past bars without `--max-bars-back`; `-q` hides it (#102).
- `el_emit` reports string or true/false arguments passed to verified numeric parameters as ARG-TYPE-BAD (#87).
- The native kernel runs numeric and true/false arrays, the common `Array_*` functions and `for` loops with `break` / `continue` (#100).
- The native kernel runs the predeclared `Value0`..`Value99` and `Condition0`..`Condition99` variables (#104).

### Changed

- CI builds the native kernel for Python 3.9, 3.12 and 3.14 and runs the native-vs-Python parity tests on each (#124).
- The frozen `el_emit` parser corpus moved from `emitted/` to `tests/fixtures/emitted/` (#115).
- The README quickstart passes `--max-bars-back 21`, so the demo prints no warm-up note (#115).
- Tests carry `needs_native`, `needs_captures` and `perf` markers; a plain pytest run deselects captures and perf tests (#115).
- The README says the Python to EasyLanguage direction accepts only the mirror dialect and explains the warm-up difference (#102).
- The README quickstart shows the Python to EasyLanguage direction (`el_emit`) (#89).
- The repository was recreated on 2026-10-22; pull-request pages from before then are not available (#89).
- `pl_oracle -v` stays user-facing; the kernel's `[accel]` internals now need `-vv` or `PB_DEBUG=1` (#99).
- The installed package no longer adds the checkout root to `sys.path` on import (#99).
- CONTRIBUTING, LIMITATIONS and ACCEL docs describe default test skips, the prebuilt accel wheels and how to tell which path ran (#99).
- `parser.py` and `py_front.py` are split into smaller modules, with byte-identical output (#108).
- `engine.py`, `codegen_el.py` and `runtime/_rt_indicators.py` are split into smaller modules, with byte-identical output (#112).
- ruff also checks import sorting, pyupgrade and bugbear rules; re-raised errors now chain their cause (#117).
- CI's mypy job type-checks the whole `pl_transpiler` package (#119).

### Fixed

- EL names that clash with Python names (such as `max` or `str`) and blocks holding only a redeclaration now transpile to valid Python (#130).
- Native `Array_Sum` and `Average` now add exactly like the running Python's `sum()`, including the 3.14 rule (#124).
- `--explain-native` no longer crashes the native kernel on a `Vars:` declared inside a block (#111).
- An undeclared `ValueN` / `ConditionN` read before it is written no longer raises `UnboundLocalError` in the Python engine, and keeps its value across bars (#103).
- Without `-o`, `el_emit`'s check report names the input file instead of a temporary file (#102).
- The native typed trace prints exactly what the Python trace prints (#93).
- The native kernel's `Mod`, division by zero and other numeric edge cases now match the Python path instead of trading on `inf`/`NaN` (#95).
- The native kernel reads bar lookbacks (`Close[n]` and so on) unrounded, as the Python runtime does (#97).

## 0.7.0 — 2026-10-22

### Added

- Drawing dump: `run_oracle(..., drawings=True)` and `pl_run` / `pl_oracle
  --drawings` record trend lines, arrows and text objects as a versioned JSON
  dump (`docs/DRAWING_DUMP.md`, schema in `ground_truth/drawing/schema/`).
  It is off by default, and with it off the trade digest is unchanged.
  `ground_truth/drawing/` holds seven drawing cases; the tests run each one on
  synthetic bars, and compare against MultiCharts readbacks only when you
  supply your own copy (it is licensed and not in the repository).
- The native kernel records drawing objects too, so `--drawings` no longer
  forces the Python path. Its dump matches the Python one except for
  `producer.kind`. Drawing calls the kernel does not implement fall back to
  Python. Anchor prices in the dump are stored as float32 on both paths.
- `TL_Get*`, `Arw_Get*` and `Text_Get*` read the recorded object. They stop the
  run with an error when drawings are not being recorded, and `TL_GetValue`
  always stops, since its interpolation is not measured.
- `#BeginCmtry … #End` blocks parse. PowerBridge has no commentary-on mode, so
  the block is kept in the AST (and written back by the EL emitter) but not run,
  on both the Python and native paths. `Commentary` and `CommentaryCL` accept
  several arguments, joined into one message.
- `A crosses over B` and `A crosses under B` are accepted as synonyms for
  `crosses above` and `crosses below`, and produce the same code. They used to
  fail with a misleading "expected 'then'" error.
- `N Point` / `N Points` in an expression means N minimum price moves
  (N * MinMove / PriceScale from the instrument config; NQ: 0.25), so
  `Buy next bar at Highest(High, 5) + 1 Point Stop;` now runs on the Python
  path. The native kernel has no MinMove/PriceScale value and refuses such
  programs loudly.
- Tests for indexed Data2 spellings (`Close[N] of Data2`, `Close of Data2[N]`,
  `Close Data2`) check that they fail loudly rather than read Data1.
- The EL keyword catalog records how much evidence backs each keyword. Each line
  of `pl_signatures.jsonl` gains `kind` (`syntax` for language and filler words
  such as `If` or `Shares`, `builtin` otherwise), `arg_types`, `max_arity`,
  `verification` and `verified_by`, all generated from PowerBridge's own code and
  tests by `tools/gen_catalog_fields.py` (a test fails if the shipped file is
  stale and prints the regenerate command). `verification` is `n/a` for syntax
  words; for builtins it is `verified:capture-strategy` (used by a strategy whose
  per-bar outputs (required columns) match a MultiCharts capture in
  `tests/verify_all.py`; the captures are not public, so that check skips in a
  clone), `verified:test` or `documented-unverified`. `max_arity` and
  `arg_types` are filled only where PowerBridge itself defines an arity and are
  `null` otherwise. `catalog.kind()`, `catalog.arg_types()`,
  `catalog.max_arity()` and `catalog.verification()` read them,
  `catalog.coverage()` counts keywords per kind and level, and
  `pl_signatures.schema.json` describes a record. Existing fields are unchanged.
- The keyword catalog fills more gaps, each checked by running PowerBridge
  (`tests/test_catalog_returns.py` runs every keyword it fixes and compares the
  value type with `catalog.returns()`). The 16 color constants and
  `OpenEntriesCount` now return `numeric` instead of `unknown`, and 84 builtins
  that were on `mc_verified_builtins.txt` without a signature record (such as
  `CurrentBar`, `Symbol`, the `Array_*` helpers and the session-time words) gain
  one. Unknown returns drop from 184 to 171; the rest are declaration and type
  words, order statements, runtime stubs and the `Array_Set*Value` setters, whose
  return value PowerBridge's own code marks as unsettled, so they stay
  `unknown`. Three verified tokens still have no record, by design: `Abort` (a
  statement), and `CommandLine` and `EntryName` (not implemented). A test pins
  these counts.
- `docs/reference/`: a keyword reference for the 257 EasyLanguage builtins
  PowerBridge supports or has evidence for, grouped by topic. Each entry shows
  the catalog's kind, return type, arity and verification level, the run status
  observed when PowerBridge runs it, a short description and, for many, an
  example. `tools/gen_reference.py` builds the pages from the catalog and
  `docs/reference/_notes.json`; a test runs every example and fails when the
  pages are stale.
- `docs/API.md`: a reference for the public library functions and the CLIs,
  with runnable examples. `pl_transpiler.__all__` lists the public names, and
  `run_oracle`, `load_bars`, `load_config_file`, `catalog` and
  `UnimplementedKeywordError` now import from `pl_transpiler` directly.
- `docs/DESIGN.md`: design notes on the architecture, the main decisions and
  their trade-offs, how verification works (public and private), and known
  limitations.
- CONTRIBUTING: where reference and catalog facts must come from (PowerBridge's
  code, tests and observed behaviour, never vendor documentation).

### Changed

- Colour words, `RGB`, `LegacyColorToRGB`, `GradientColor` and the `Tool_*`
  line styles now use the values measured in MultiCharts (for example Blue is
  16711680 and Tool_Solid is 1). Colours and calls without a measurement stop
  with an error instead of returning a guess.
- `pl_run` and `pl_oracle` print the "powerbridge-accel not installed" note
  only with `-v` / `--verbose` (new on `pl_run`). Notes about an installed
  kernel that refuses a program or has the wrong version still always print.
  The note now names the full docs/ACCEL.md#install URL instead of
  "README#acceleration", which a terminal cannot open.
- The README is cut to a pitch, install, a short quickstart and links; the
  walkthrough, CSV format and engine assumptions moved to `docs/USAGE.md`, the
  limitations to `docs/LIMITATIONS.md`, and the accel install and benchmarks to
  `docs/ACCEL.md`.
- `Sell` / `BuyToCover ... from entry("X")` that would fill while more than one
  entry is open in that direction now stops the run instead of closing the whole
  position, since the per-entry MultiCharts fills are unverified (#24).
  `--lenient` keeps the old behaviour with a warning; one open entry is unchanged.

### Fixed

- Inside a strategy, `NumWinTrades`, `NumLosTrades`, `NumEvenTrades`,
  `PercentProfit`, `LargestWinTrade`, `LargestLosTrade`, `MaxConsecWinners`,
  `MaxConsecLosers`, `TotalBarsWinTrades`, `TotalBarsLosTrades`,
  `TotalBarsEvenTrades`, `AvgBarsWinTrade`, `AvgBarsLosTrade` and
  `AvgBarsEvenTrade` always read 0. The engine counted closed trades but never
  passed those counts to the strategy. They now update on every bar, at the same
  point as `TotalTrades` and `GrossProfit`. The native kernel still refuses these
  words and falls back to the Python engine with a message.
- `A crosses above B` and `crosses below` on the Python path silently never
  fired when an operand was not a series: a function call such as
  `Average(Close, 10)`, a literal, or an offset such as `Close[3]`. Each such
  operand now keeps its own hidden history, filled at the start of every bar, so
  the strategy trades exactly as if the operand were first assigned to a
  variable. `Value1`..`Value99` used directly as an operand also never fired;
  they now keep their history like a declared variable starting at 0. An operand
  that can change within the bar (such as `Value1 + 1` or `MarketPosition`) is
  refused at transpile time, with its line, and the runtime raises instead of
  returning false when it is handed a single value. The native kernel still
  refuses these operands and falls back to the Python path with its usual
  message.
- `crosses above` / `crosses below` against a constant level now fire. A literal
  (`X crosses above 0`) was treated as "no series" and never crossed, and a side
  that is all zeros (a declared `Z(0)`) was skipped as warm-up padding, so the
  cross never started. Leading zeros are now skipped only on a side that has a
  nonzero value; the warm-up skip for indicator pairs is unchanged, and so are
  all existing digests. The native kernel computes crosses the same way.
- A stray word after an order price (for example the unsupported
  `+ 2 Ticks Stop`) is now a syntax error that names that word, instead of an
  "unimplemented keywords" report that listed the valid `stop` as well.
- The `docs/USAGE.md` sections "Python (mirror dialect) to EasyLanguage", "Your
  own data" and "Other instruments" create `out/` themselves, so they no longer
  fail with FileNotFoundError when run on their own; the mirror section shows
  `el_emit`'s full output. The docs test now clears `out/` at each section.

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
