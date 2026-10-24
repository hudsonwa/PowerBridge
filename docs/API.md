# API reference

PowerBridge's Python library and command-line tools. Every example below runs
from the repository root against the files in `examples/` (`ma_cross.el`, a
moving-average cross strategy, and `NQ_sample_bars.csv`, 140 synthetic one-minute
bars). The outputs shown are what the examples print.

- [Library](#library): [`transpile`](#transpile), [`run_el`](#run_el),
  [`run_oracle`](#run_oracle), [`load_bars_csv` / `load_bars`](#load_bars_csv--load_bars),
  [`get_config` / `load_config_file`](#get_config--load_config_file),
  [`catalog`](#catalog), [errors](#errors)
- [Command-line tools](#command-line-tools): [`pl_transpile`](#pl_transpile),
  [`pl_run`](#pl_run), [`pl_oracle`](#pl_oracle), [`el_emit`](#el_emit),
  [`catalog`](#python--m-pl_transpilercatalog)

## Library

All public names import from the package root and are listed in
`pl_transpiler.__all__`:

```python
from pl_transpiler import (transpile, run_el, run_oracle, load_bars_csv, load_bars,
                           get_config, load_config_file, catalog,
                           UnimplementedKeywordError)
from pl_transpiler import ELRuntimeError
```

### Bars and dates

A bar is a dict with the keys `date`, `time`, `open`, `high`, `low`, `close`,
`volume`. `date` and `time` are EasyLanguage integers: `date` is `YYYMMDD`
where `YYY` is the year minus 1900 (`1240102` is 2 January 2024), and `time`
is `HHMM` (`930` is 09:30). Prices and volume are floats. Wherever a bar list
is accepted, a memory-mapped `BarTape` returned by `load_bars` for a `.pbar`
file works too.

### transpile

```python
transpile(pl_source: str, trace: bool = False, partial: bool = False,
          persist: bool | None = None, dialect: str = "runnable") -> str
```

Converts EasyLanguage / PowerLanguage source into Python source. The default
`dialect="runnable"` is the code `pl_transpile` writes and `pl_run` executes:
EL Variables live in a cross-bar `_state` dict, so the strategy can be driven
bar by bar. `dialect="mirror"` emits the clean, stateless dialect that
`el_emit` reads back into EasyLanguage; Variables reset every bar, so it is not
for backtesting and cannot be combined with `trace` or `persist`.
`trace=True` adds per-bar capture of variable values (what `run_el` uses).
An EL keyword the transpiler does not implement raises
`UnimplementedKeywordError`; `partial=True` instead turns such keywords into
stubs that raise only if executed, under a NOT-FAITHFUL watermark.
`persist=False` is an older spelling of `dialect="mirror"`.

```python
from pl_transpiler import transpile

src = open("examples/ma_cross.el").read()
py = transpile(src)
print(py.splitlines()[0])
```

```text
from pl_transpiler.runtime.pl_runtime import f32, el_round0, el_round, pl_file
```

### run_el

```python
run_el(el_source: str, bars, config, *,
       trace_columns: Sequence[str] | None = None, partial: bool = False,
       row_sink: Callable[[list[str]], Any] | None = None,
       header_sink: Callable[[list[str]], Any] | None = None,
       retain_rows: bool | None = None,
       from_date: int | None = None, from_bar: int | None = None,
       seed_warmup: bool = True, max_bars_back: int | str | None = None,
       max_entries_per_direction: int | None = None) -> dict
```

Transpiles `el_source`, runs it over `bars` with the instrument `config`, and
returns the per-bar trace and the trade list. This is the library form of
`pl_run`. Import it from the package root, or as
`from pl_transpiler.run import run_el`, where the code lives;
`from pl_transpiler.tools.pl_run import run_el` keeps working.

| Keyword | Default | Meaning |
|---|---|---|
| `trace_columns` | `None` | Columns to emit. `None` uses every trace key of the first bar, sorted. Names match case-insensitively, ignoring underscores, and accept EL spellings such as `MarketPosition`, `NetProfit`, `CurrentBar`, `Close`, `Date`, `Time`, plus strategy variables. An unknown name raises `ValueError` on the first bar. |
| `partial` | `False` | Turn unimplemented EL keywords into stubs (raising only if evaluated) and print the stub list to stderr before the run, instead of refusing to transpile. Results are not faithful. |
| `row_sink` | `None` | Called once per emitted bar with that bar's row (`list[str]`), for streaming. |
| `header_sink` | `None` | Called once with the column list when it is first known. |
| `retain_rows` | `None` | Keep rows in the result's `rows`. `None` means keep them unless `row_sink` is given, so streaming callers do not also hold every row in memory. |
| `from_date` | `None` | Inclusive window start as an EL date (`YYYMMDD`). Default is the full tape. |
| `from_bar` | `None` | 0-based window start index. Mutually exclusive with `from_date`. |
| `seed_warmup` | `True` | With a window, run the bars before it with fills suppressed so `Once`, lookbacks and Variables are primed. `False` slices them off instead. |
| `max_bars_back` | `None` | Treat the first N bars as history only: no evaluation, no orders, no rows, and BarNumber starts at 1 on bar N+1. `"auto"` uses the lookback inferred from the source and raises `ValueError` when it cannot. The default `None` is `"auto"` falling back to 0 when the lookback cannot be inferred; pass `max_bars_back=0` for the results of 0.10.0 and earlier. |
| `max_entries_per_direction` | `None` | `None`/`0`: every Buy/SellShort adds to the position. N ≥ 1: an entry in the open position's direction is refused while N entries are open. Reversals always fill. |

The result is a dict:

| Key | Type | Content |
|---|---|---|
| `columns` | `list[str]` | Column names, as requested in `trace_columns` (or the trace keys). |
| `rows` | `list[list[str]]` | One row per evaluated bar, aligned with `columns`. Empty when rows are not retained. |
| `trades` | `list[dict]` | Fills in order. Each has `order_no` (int), `type` (`"EntryLong"`, `"ExitLong"`, `"EntryShort"`, `"ExitShort"`), `signal` (order name), `date`, `time` (EL ints), `price` (float), `contracts` (int). |
| `n_rows` | `int` | Rows emitted, whether or not they were retained. |
| `n_warmup` | `int` | Bars run as seeded warmup (0 without a window or with `seed_warmup=False`). |
| `unimplemented` | `dict[str, int]` | Builtins the runtime does not implement that the run evaluated as `0`/`''` (only when strict mode is off, e.g. `PL_STRICT=0`), with call counts. Empty in the default strict mode, which stops instead. |

Every cell in `rows` is a string: `str(value)` of the traced value, and `"0"`
for a value that is missing on that bar. Strings keep the full precision of
the engine's floats and make each row directly writable as CSV, so results
can be compared byte for byte. Convert with `float()` when you need numbers.
The `pl_run` CLI writes the same rows but, for readability, rounds money
columns (`NetProfit`, `GrossProfit`, `GrossLoss`, `OpenPositionProfit`,
`PositionProfit`, `MaxIDDrawdown`) to cents and price columns (`Close`,
`EntryPrice`, `ExitPrice`, `AvgEntryPrice`) to the instrument's tick decimals;
`--full-precision` turns that off. `run_el` itself never rounds.

```python
from pl_transpiler import run_el, load_bars_csv, get_config

out = run_el(open("examples/ma_cross.el").read(),
             load_bars_csv("examples/NQ_sample_bars.csv"),
             get_config("NQ"),
             trace_columns=["Close", "MarketPosition", "NetProfit"])
print(out["columns"], out["rows"][-1])
print(out["n_rows"], "rows,", len(out["trades"]), "fills")
print(out["trades"][0])
```

```text
['Close', 'MarketPosition', 'NetProfit'] ['15001.9', '1', '1343.7984375000087']
140 rows, 3 fills
{'order_no': 1, 'type': 'EntryLong', 'signal': 'LE', 'date': 1240102, 'time': 1030, 'price': 14961.08, 'contracts': 1}
```

### run_oracle

```python
run_oracle(el_source: str, bars, config, *,
           data2_closes: Sequence[float] | None = None, data2_bars=None,
           from_date: int | None = None, to_date: int | None = None,
           from_bar: int | None = None, seed_warmup: bool = False,
           use_native: bool = False, digest_version: int = 2,
           max_bars_back: int | str | None = None,
           max_entries_per_direction: int | None = None) -> dict
```

Runs the strategy without per-bar tracing and returns a canonical digest of
its trade list: the fastest way to check that two runs (or two engines)
produce exactly the same trades. This is the library form of `pl_oracle`.
Import it from the package root, or as
`from pl_transpiler.oracle import run_oracle`, where the code lives;
`from pl_transpiler.tools.pl_oracle import run_oracle` keeps working.
`from_date` / `to_date` / `from_bar` window the tape (by slicing, unless
`seed_warmup=True`). `data2_bars` (or `data2_closes`, a Close series aligned
with `bars`) supplies Data2. `use_native=True` tries the optional Rust kernel
and falls back to Python with a stderr note when it cannot serve the run; the
digest is the same either way. `digest_version=1` reproduces the older v1
digest. `max_bars_back` and `max_entries_per_direction` mean the same as in
`run_el`.

The result has `text` (the digest as printed by `pl_oracle`), `sha256` (str),
`fills` and `round_turns` (int), `n_bars` and `n_warmup` (int), `records`
(the fill list, as `run_el`'s `trades`), `closed_positions` (list, or `None` on
the native path), `native` (bool: the kernel produced the digest),
`native_fallback_reason` (str or `None`), `fast_path_hits` and
`unimplemented`.

```python
from pl_transpiler import run_oracle, load_bars, get_config

d = run_oracle(open("examples/ma_cross.el").read(),
               load_bars("examples/NQ_sample_bars.csv"), get_config("NQ"))
print(d["text"], end="")
```

```text
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

### load_bars_csv / load_bars

```python
load_bars_csv(path, date_format: str | None = None) -> list[dict]
load_bars(path, prefer_pbar: bool = True, date_format: str | None = None)
```

`load_bars_csv` reads a plain OHLCV CSV into the bar list `run_el` and
`run_oracle` take. The header must contain `date,time,open,high,low,close,volume`
(any order, case-insensitive; extra columns are ignored). Dates are either EL
integers (`1240102`) or slash dates (`2/1/2024`), and times are `HHMM` or
`HH:MM[:SS]`; both are normalised to EL integers. `date_format` (`"dmy"`,
`"mdy"` or `"ymd"`) sets the order of slash dates.
The default `None` infers it from the whole file (a first field above 12 is
day-first, a second field above 12 month-first) and raises `ValueError` when
no field is above 12 or both orders occur.
A bad row raises `ValueError` naming the line.

`load_bars` also reads `.pbar` files, a memory-mapped binary form that opens
far faster than parsing a large CSV. Given a CSV path, it uses the sibling
`<stem>.pbar` when that exists and is not older than the CSV (a stale one is
ignored with a stderr note); set `PB_NO_PBAR=1` or `prefer_pbar=False` to
always parse the CSV. Build a `.pbar` with
`python -m pl_transpiler.runtime.bar_tape csv2pbar`.

```text
date,time,open,high,low,close,volume
1240102,930,14997.50,15005.00,14992.50,15000.00,125
```

```python
from pl_transpiler import load_bars_csv

bars = load_bars_csv("examples/NQ_sample_bars.csv")
print(len(bars), bars[0])
```

```text
140 {'date': 1240102, 'time': 930, 'open': 14997.5, 'high': 15005.0, 'low': 14992.5, 'close': 15000.0, 'volume': 125.0}
```

### get_config / load_config_file

```python
get_config(name: str = "NQ") -> InstrumentConfig
load_config_file(path) -> InstrumentConfig
```

An `InstrumentConfig` describes the traded instrument (big point value, tick,
price scale, session start, symbol names) and drives fill prices and profit.
`get_config` returns a built-in config (`"NQ"`, `"ES"`) or, given a path ending
in `.json` or naming an existing file, loads it with `load_config_file`.
`load_config_file` reads a JSON object whose keys are exactly the
`InstrumentConfig` fields (`category` and `round_turn_cost` optional); unknown
or missing keys, wrong types and a negative `round_turn_cost` raise
`ConfigFileError`. `examples/configs/NQ.json` and `ES.json` are templates.

```python
from pl_transpiler import get_config, load_config_file

nq = get_config("NQ")
print(nq.bigpointvalue, nq.tick, load_config_file("examples/configs/NQ.json") == nq)
```

```text
20 0.25 True
```

### catalog

`pl_transpiler.catalog` answers questions about the bundled EasyLanguage
keyword catalog (`catalog.coverage()` gives the counts). Names are case-insensitive.

| Function | Returns |
|---|---|
| `get(name)` | The record dict (`keyword`, `kind`, `returns`, `min_arity`, `zero_arg_property`, `arg_types`, `max_arity`, `verification`, `verified_by`, `verified`), or `None`. |
| `kind(name)` | `"syntax"` for a language or filler word, else `"builtin"`; `KeyError` if unknown. |
| `arity(name)` | `(min_arity, zero_arg_property)`; `KeyError` if unknown. |
| `max_arity(name)` | Largest argument count accepted, or `None` when PowerBridge does not define one; `KeyError` if unknown. |
| `arg_types(name)` | Per-argument types (`"numeric"` or `"any"`), or `None` when unknown; `KeyError` if unknown. |
| `returns(name)` | Declared return type (`"numeric"`, `"string"`, ...); `KeyError` if unknown. |
| `verification(name)` | Evidence level: `"verified:capture-strategy"`, `"verified:test"`, `"documented-unverified"` or `"n/a"`; `KeyError` if unknown. |
| `is_verified(name)` | Whether the keyword is on the verified-builtin list. |
| `search(substring)` | Sorted keyword names containing `substring`. |
| `coverage()` | Counts: `signatures`, `verified_builtins`, `verified_with_signature`, `kind:<kind>`, `verification:<level>`, `with_max_arity`. |

`verified` (the bool from `get()` / `is_verified()`) only means the name is on the
verified-builtin list; `verification` is the separate evidence level for PowerBridge's
own behaviour. `verified:capture-strategy` means a ground-truth strategy using the
keyword has per-bar outputs (required columns) that match a MultiCharts capture in
`tests/verify_all.py` (the captures are not public, so that check skips in a clone);
`verified:test` means EL embedded in a PowerBridge test calls it. Record fields are
described in
[`pl_transpiler/tools/pl_signatures.schema.json`](../pl_transpiler/tools/pl_signatures.schema.json).

```python
from pl_transpiler import catalog

print(catalog.arity("MarketPosition"), catalog.returns("MarketPosition"))
print(catalog.search("crosses"))
print(catalog.kind("SquareRoot"), catalog.max_arity("SquareRoot"),
      catalog.arg_types("SquareRoot"), catalog.verification("SquareRoot"))
```

```text
(0, True) numeric
['Crosses']
builtin 1 ['numeric'] verified:capture-strategy
```

### Errors

- `UnimplementedKeywordError`: the source uses EL the
  transpiler does not implement. The message lists each keyword with its line;
  `.errors` holds the details.
- `ELRuntimeError`: raised by `run_el` and `run_oracle` when the program
  fails while it runs: an array index out of range, a division (or `Mod`) by
  zero, or any other exception from the generated code. `.line` is the EL line
  of the failing statement (`None` if unknown), the message is one line such as
  `array index 4 out of range for 'a' (max index 3) at line 2`, and the
  original Python exception is its `__cause__`. It is also an instance of
  that exception's type, so existing `except IndexError` /
  `except ZeroDivisionError` handlers keep working.
- `ConfigFileError` (a `ValueError`, in `pl_transpiler.runtime.instrument_config`):
  a malformed instrument config file.
- `ValueError` for bad bar files, unknown trace columns and invalid options.

## Command-line tools

Installing the package provides `pl_transpile`, `pl_run`, `pl_oracle` and
`el_emit`; each also runs as `python -m pl_transpiler.tools.<name>`. All accept
`--help` and `--version`.

### pl_transpile

EasyLanguage → Python. Writes the runnable dialect (the same as `transpile()`).

| Flag | Meaning |
|---|---|
| `-o FILE` | Write Python here (default: stdout). |
| `--trace` | Emit trace-mode Python (per-bar variable capture). |
| `--no-persist` | Emit the mirror dialect, the input for `el_emit`; not runnable as a backtest. |
| `--dialect {runnable,mirror}` | Output dialect, named as in `transpile(src, dialect=...)`. `runnable` (default) runs as a backtest; `mirror` is the input for `el_emit` and is not runnable as a backtest (the same as `--no-persist`, which stays as an alias). `--trace` always emits `runnable`. |
| `--partial` | Unimplemented constructs become execution-time stubs under a NOT-FAITHFUL watermark. |

```sh
pl_transpile examples/ma_cross.el -o ma_cross.py
```

### pl_run

Runs a strategy over bars and writes one CSV row per bar (columns from
`--columns`, default all trace columns). Money and price cells are rounded for
display (see [`run_el`](#run_el)).

| Flag | Meaning |
|---|---|
| `bars_csv` | OHLCV CSV or `.pbar`. |
| `--config NAME\|FILE` | Built-in instrument (`NQ`, `ES`) or a JSON config file. Default `NQ`. |
| `-o FILE` | Write the CSV here (default: stdout). |
| `--columns A,B,...` | Trace columns to emit. |
| `--date-format dmy\|mdy\|ymd` | Field order of slash dates in the CSV. The default infers it from the file and stops with an error (exit 2) when it cannot tell. |
| `--from-date YYYMMDD` / `--from-bar N` | Start the output at a date or bar index, with seeded warmup. |
| `--slice` | Drop the bars before the window instead of seeding warmup. |
| `--max-bars-back N\|auto` | First N bars are history only; `auto` uses the lookback found in the source. The default is `auto`, or 0 with a stderr note when the lookback cannot be found. |
| `--max-entries-per-direction N` | Refuse same-direction entries while N are open. Default unlimited. |
| `--full-precision` | Do not round money and price cells. |
| `--lenient` | Keep going instead of stopping on something not supported yet: an unimplemented builtin returns `0` (`''` for a text builtin), a missing bar column such as `Ticks` reads as `0`, and a `from entry` exit with several named entries open closes the whole position. A `WARNING` at the end names each one. Same as `PL_STRICT=0`. |
| `--partial` | As in `pl_transpile`; results are not faithful. |

```sh
pl_run examples/ma_cross.el examples/NQ_sample_bars.csv \
    --columns Date,Time,Close,MarketPosition,NetProfit | tail -1
```

```text
1240102,1149,15001.90,1,1343.80
```

### pl_oracle

Prints the trade-list digest (see [`run_oracle`](#run_oracle)). Uses the Rust
kernel when it is installed.

| Flag | Meaning |
|---|---|
| `--config`, `--date-format`, `--max-bars-back`, `--max-entries-per-direction` | As in `pl_run`. |
| `--from-date` / `--from-bar` / `--to-date` | Window the tape (sliced unless `--seed-warmup`). |
| `--seed-warmup` | Run the bars before the window with fills suppressed instead of dropping them. |
| `--data2 FILE` | Aligned Data2 bars. |
| `--native` / `--python` | Prefer the Rust kernel (default when installed) / force pure Python. |
| `-o FILE` | Also write the digest here. |
| `--compare FILE` | Exit 1 if the digest differs from this file. |
| `--fills-csv FILE` | Write a readable fill log. |
| `--dump FILE` | Write the hashed payload. |
| `--digest-v1` | Emit the older v1 digest (Python path only). |
| `--lenient`, `-v` | As in `pl_run`; `-v` prints kernel diagnostics. |

```sh
pl_oracle examples/ma_cross.el examples/NQ_sample_bars.csv
```

```text
powerbridge-trade-digest v2
fills: 3
round_turns: 1
sha256: f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da
```

A one-line summary (`[pl_oracle] 140 bars, 3 fills, ...`) goes to stderr.

### el_emit

Python (mirror dialect) → EasyLanguage. A `.py` input is converted; any other
input is parsed as EL and re-emitted in canonical form. After the EL it
prints a check of every builtin used against the keyword catalog (names,
argument counts, return types, and argument types where the catalog declares a
verified `numeric` parameter and the argument's type is certain). `-o FILE` writes the EL to a file
instead of stdout.

```sh
pl_transpile --no-persist examples/ma_cross.el -o ma_mirror.py
el_emit ma_mirror.py | head -8
```

```text
Inputs: fastlen( 9 ) ;
Inputs: slowlen( 21 ) ;
Vars: fast( 0 ) ;
Vars: slow( 0 ) ;
fast = average(close, fastlen) ;
slow = average(close, slowlen) ;
If fast crosses above slow Then Buy ( "LE" ) next bar at market ;
If fast crosses below slow Then Sell ( "LX" ) next bar at market ;
```

### python -m pl_transpiler.catalog

Looks up the keyword catalog from the shell: a keyword name prints its record
as JSON, `--search SUB` lists matching keywords, `--coverage` prints the counts.

```sh
python -m pl_transpiler.catalog --search crosses
```

```text
Crosses
(1 match)
```
