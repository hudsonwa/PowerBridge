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


# The optional `accel` fast path

The core is **standard library only** in every install. The optional native
fast path is a Rust crate in `accel/` that builds the module
`pl_transpiler._accel`. It runs the whole bar loop and fill engine natively in
one FFI call (`pl_transpiler.runtime.accel.run_native`), plus batched input
sweeps (`run_native_sweep`) that compile and marshal the tape once.

`powerbridge-accel` is not on PyPI yet (coming); install it from a release
wheel or from source as below.

## Install

**Prebuilt wheel (no Rust needed).** Each GitHub Release from v0.3.0 on
carries one `powerbridge-accel` wheel per platform, built for CPython 3.9 and
newer (abi3). The asset URL follows this pattern:

```
https://github.com/hudsonwa/PowerBridge/releases/download/vX.Y.Z/powerbridge_accel-X.Y.Z-cp39-abi3-PLATFORM.whl
```

Pick the file on the release page whose name ends with your platform:

| Platform | File name ends with |
|---|---|
| macOS Apple Silicon | `macosx_*_arm64.whl` |
| macOS Intel | `macosx_*_x86_64.whl` |
| Windows x64 | `win_amd64.whl` |
| Linux x86_64 | `manylinux*_x86_64.whl` |
| Linux aarch64 | `manylinux*_aarch64.whl` |

Then install core and the wheel, using the exact file URL:

<!-- readme-test: skip (template URL; installs over the network) -->
```bash
pip install "powerbridge @ git+https://github.com/hudsonwa/PowerBridge@vX.Y.Z"
pip install https://github.com/hudsonwa/PowerBridge/releases/download/vX.Y.Z/<file>.whl
```

**Verify the download (optional).** Releases after v0.11.0 carry a `SHA256SUMS` file covering every sdist and
wheel, and a GitHub build provenance attestation for each of those files.
Download the file you want and `SHA256SUMS` from the same release into one
folder, then run:

<!-- readme-test: skip (needs downloaded release files and the gh CLI) -->
```bash
sha256sum -c SHA256SUMS --ignore-missing   # macOS: shasum -a 256 -c SHA256SUMS --ignore-missing
gh attestation verify <file> --repo hudsonwa/PowerBridge
```

The first checks the file was not changed after the release was built; the
second checks it was built by this repository's `release.yml` workflow.
v0.11.0 and earlier have neither.

**Check your install.** Run `pl_oracle --version` (or `pl_run --version`).
The second line says whether the kernel will be used:

```text
pl_oracle 0.13.0
native kernel: powerbridge-accel 0.13.0 (available)
```

If it says `not available`, the reason follows in brackets: `not installed`,
`import failed: ...` (often a wheel for another platform) or a version mismatch
with `powerbridge`. From Python, `pl_transpiler.runtime.accel.native_status()`
returns `(available, line)` with the same line.

**From source.** Build it from a checkout (needs a Rust toolchain):

<!-- readme-test: skip (needs a Rust toolchain) -->
```bash
pip install .                                    # core (pure stdlib)
pip install maturin
(cd accel && maturin build --release)            # accel wheel
pip install accel/target/wheels/powerbridge_accel-*.whl      # the native module
python -I -c "import pl_transpiler._accel as a; print(a.ping())"  # 42; -I keeps the checkout's pl_transpiler/ off sys.path
```

Working inside the repo instead? `./tools/build_accel.sh` builds the same
library and drops it next to the in-tree package (`pl_transpiler/`), so tests
run against it. The installed console commands (`pl_run`, `pl_oracle`, ...)
only see it if the package is installed in editable mode; a plain `pip install .`
copies the package into site-packages and keeps reporting "accel not installed".
Use:

<!-- readme-test: skip (needs a Rust toolchain) -->
```bash
pip install -e .            # editable: console commands use the in-tree package
./tools/build_accel.sh      # PYTHON=... to build for a specific interpreter
```

With a non-editable install, use the wheel route above instead.

## Speed

Indicative speed, not a promise. `python tools/bench_native.py --bars N`
times the GT2 sample strategy on a synthetic tape on both paths (median of 3
runs, whole `run_el` vs `run_native` call) and checks the fills are identical.
Measured on an Apple M4 laptop, CPython 3.14, release-lto build from
`./tools/build_accel.sh`:

| Bars | Fills | Python | Rust | Speedup |
|---:|---:|---:|---:|---:|
| 20,000 | 1,473 | 0.47 s | 0.087 s | 5.3x |
| 100,000 | 7,671 | 2.34 s | 0.42 s | 5.5x |

Both paths scale linearly with the bar count (the earlier super-linear
slowdowns of the Python path, issue #4 and a lagged function call such as
`Highest(High, Len)[1]`, are fixed). The native kernel does not run a lagged
function call yet; such a strategy runs on the Python path. Measure your own
strategy before relying on a ratio.

Arrays and loops run natively when they are numeric or true/false arrays with
literal sizes and `for` loops; the kernel copies the Python path's list
semantics, including its quirks (a fractional index truncates toward zero; a
negative index, or one past the end, stops the run). String arrays, `Array_Sort`,
`Array_GetType`, `VarianceArray`, `Array_Compare`, `while` / `repeat` loops and
a typed trace of a program with arrays are refused and run on the Python path.
On Python 3.12 and later `Array_Sum` adds floats the way that Python's `sum()`
does; when the kernel cannot tell whether an element is an int or a float
there, it refuses. `Average` follows the same rule; that refusal is decided
when the program is compiled, so `--explain-native` lists it as
`function Average` (#128).

A `Vars:` declared inside a `begin ... end` block (an `if` or `else` branch,
`Once`, a `for` body) runs natively as the Python path runs it: the variable
is a local of that bar, set to its initial value (which may be any
expression) every time the declaration runs, with no float32 rounding, no
trace column and nothing kept to the next bar. The same name may be declared
in several blocks. Redeclaring a top-level variable inside a block changes
nothing, as in Python. The kernel refuses, at compile time, the uses the
Python path cannot run or runs differently: reading or assigning the
variable before its declaration or outside the block that declares it, a
lookback (`Z[1]`) or series use (`Average(Z, 3)`) of it, a block name that is
also an input, array or price series, and redeclaring a top-level variable
that is read as a series or with a lookback, or with an initial value that is
not a literal (#128).

The session-time words run natively with the Python path's template
constants, whatever their arguments: `SessionEndTime(...)` (1600),
`SessionStartTime(...)` (1700), `SessionEndTimeMS(...)`, `Sess1EndTime` and
`Sess2EndTime` (1515), `SessionStartTimeMS(...)` and `Sess2StartTime` (830),
and `Sess1FirstBarTime` / `Sess2FirstBarTime` (831). Bare `SessionEndTime`,
`SessionStartTime` and `Sess1StartTime` (no parentheses) read run values on
the Python path instead and are refused.

`CurrentSession` (with or without arguments) runs natively as the ISO
weekday of the bar's date, 1 = Monday to 7 = Sunday: the value the Python bar
loop passes in. Its arguments are evaluated and ignored, so a session number
does not change the result. `SessionCount(...)` and `SessionCountMS` return
5, and `SessionEndDay(...)` and `SessionEndDayMS(...)` return 1, also with or
without parentheses. `SessionStartDay(...)`, whose result depends on its
session-number argument, is still refused.

`Round(x, n)` runs natively with the Python path's `el_round`: half away from
zero on `x * 10 ** n`, so `Round(-2.5, 0)` is -3 and a negative `n` rounds to
tens, hundreds and so on (`Round(1234.5, -2)` is 1200). Where Python raises
(a NaN or infinite value, or `10 ** n` overflowing or underflowing to zero)
the kernel stops the run too. An integral `n` above 22 is refused, because
Python's exact integer `10 ** n` cannot be matched in floating point.
`MinMove` and `PriceScale` read the run's instrument config (`minmove`,
`pricescale`), the values the Python bar loop passes in, and
`LastBarOnChart_s` is `True`, the runtime's constant.

`Floor(x)` is Python's `math.floor`: an integer, so `Floor(-2.5)` is -3 and
there is no negative zero; a NaN or infinite value stops the run, as it raises
in Python. `ELDateToDateTime(d)` runs the runtime's `pl_datetojulian` on
`int(d)` (days since 1899-12-30, with Python's floor division, so an
out-of-range month or day gives the same number on both paths), and
`ELDateToDateTime(d, t)` is `d * 10000 + t`. `CurrentTime_s`,
`CurrentDate_s` and `ComputerDateTime` read the computer clock: the kernel
calls the runtime's `_rt_datetime._clock_now()` on every use, so both paths
share one time source and a test that patches it pins the native run too.
`CurrentDate` and `CurrentTime` are the bar's date and time on both paths.

The `total` order modifier runs natively. The Python engine does not read
it, so an order with `total` fills exactly like the same order without it.
`Sell` / `BuyToCover ... from entry ("X")` also runs natively and closes its
side as an untagged exit does. The label must be a string literal; a computed
label is refused (`order from entry` in `--explain-native`). When the named
exit would fill while more than one entry is open on its side and one of
them has a different label, the Python
engine stops (strict) or warns and closes the whole side (lenient, #24). The
kernel refuses the run at that fill, so the caller falls back to Python and
gets the same result (#125).

A typical logging section runs natively (#138). `StrLen(s)` is Python's
`len(s)`, a count of code points (`StrLen("été")` is 3). `NewLine` is `"\n"`
(LF only) unless a variable or input of that name shadows it.
`FormatDate(fmt, dt)` is an exact port of the Python formatter: a `dt` between
-693594 and 10,000,000 is a serial date (days since 1899-12-30), anything else
is `YYYYMMDD`, and the time of day is rounded to whole seconds half-to-even;
`dddd` and `MMMM` are English names. `Text_SetColor` records the colour like
`TL_SetColor`. `FileAppend` and `FileDelete` evaluate their arguments and
return 0: the Python runtime's stubs write nothing, so neither path creates,
changes or deletes a file. Python lists them as cosmetic, so they never
appear in the run's `unimplemented` report and are not part of the digest.

Python never converts a string to a number implicitly, so the kernel no
longer does either. `"a" + 1`, `"5" * 2`, comparing a string with a number,
`StrLen` of a number, `File` of a number, a `NumToStr` precision that is
negative or fractional, a `FormatDate` whose format is not a string or whose
datetime is not a number, and a datetime Python cannot decode (NaN, a serial
outside years 1 to 9999, an invalid `YYYYMMDD` date) all make the kernel
refuse the run, and the Python path runs instead. A program that uses strings
keeps every instruction and evaluates every bar, so such an error is reached
on the same bar on both paths. Still refused: string arrays, `StrToNum`,
`InStr`, `MidStr`, `LeftStr` / `RightStr`, `UpperStr` / `LowerStr`,
`FormatTime`, `Text_SetString` and the other `Text_*` setters, and
the drawing getters such as `TL_GetValue`.

## Fallback and platforms

**Absent kernel = identical behaviour.** Without the module, the auditable
Python path runs and produces the same trades. Programs the native kernel
cannot execute faithfully raise `AccelRefused` (`AccelUnavailable` when the
module is missing) so the caller runs the Python path instead; they
never return a different result. CI builds and runs macOS arm64; the release
workflow builds and smoke-tests every platform in the table above. Windows 11 x64 was
checked by hand (rustup `x86_64-pc-windows-gnu` toolchain, no Visual Studio
needed); building from source on Linux is not CI-verified.

An installed module is found even when you run from inside the repository
folder. `./tools/build_accel.sh` builds it straight into the checkout instead.

The original design record for the fast path, written before the kernel existed, is
in [design/accel-decision.md](design/accel-decision.md).

## How to tell which path ran

No feature is a silent feature. `pl_oracle` ends its stderr summary with the
path that produced the digest, `[native]` or `[python]`:

```text
[pl_oracle] 140 bars, 3 fills, 1 round-turns [native]
```

`pl_run` uses the kernel's typed per-bar trace for full-tape runs, including
`--max-bars-back` and `--max-entries-per-direction` (`--python` forces Python;
the CSV is byte-identical either way, and its status line ends `[native]` or
`[python]`). Windowed and `--partial` runs use the Python path, with a `note:`
line. A kernel that is installed but refuses a
program falls back to Python with a one-line note on stderr; it never crashes and
never silently produces a different result. Verbosity is opt-in:

- `-v` adds the "powerbridge-accel not installed" note (when it is not) and how
  many bars skipped fill processing.
- `-vv` (`pl_oracle` only) also prints native-kernel internals, the `[accel]`
  lines such as the skip-region plan. `PB_DEBUG=1` turns those on too.

## Which constructs the kernel refuses

A refusal names only the first construct the kernel hits. To see every one,
ask the kernel's compile pass directly; nothing runs and no bars are needed:

```bash
pl_oracle --explain-native my_strategy.el
```

```text
statement while 3
statement switch 8
function xaverage 11
```

Each line is `kind name line`: kind is `function`, `statement`, `order`,
`variable` or `expression` (a refused keyword is named by its EL spelling,
e.g. `expression MaxPositionProfit 2`), and line is the 1-based EL line of
the statement.
Lines are sorted by line. A program the kernel accepts prints
`native kernel can run this program (compile-time check only; a native run
can still be refused at run time)`; with refusals, that note goes to stderr.
Both exit 0. Without the kernel the
command prints `native kernel not installed` on stderr and exits 2.

From Python, `pl_transpiler.runtime.accel.explain_native(el_source)` returns
the same list as `NativeRefusal(kind, name, line, message)` records, in the
kernel's compile order; the first `message` is the `AccelRefused` text a run
raises. Only the compile pass is checked: a run can still be refused for
run-time reasons (Data2 referenced but no Data2 file given, or the reverse).
When one statement has two refused constructs, only the first is listed.
