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

**Check your install.** Run `pl_oracle --version` (or `pl_run --version`).
The second line says whether the kernel will be used:

```text
pl_oracle 0.9.0
native kernel: powerbridge-accel 0.9.0 (available)
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
semantics, including its quirks (a negative index counts from the end, an
index past the end stops the run). String arrays, `Array_Sort`,
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
`FormatTime`, `Print`, `Text_SetString` and the other `Text_*` setters, and
the drawing getters such as `TL_GetValue`.

## Fallback and platforms

**Absent extra = identical behaviour.** Without the module, the auditable
Python path runs and produces the same trades. Programs the native kernel
cannot execute faithfully raise `AccelRefused` (`AccelUnavailable` when the
module is missing) so the caller runs the Python path instead; they
never return a different result. CI builds and runs macOS arm64; the release
workflow builds and smoke-tests every platform in the table above. Windows 11 x64 was
checked by hand (rustup `x86_64-pc-windows-gnu` toolchain, no Visual Studio
needed); building from source on Linux is not CI-verified.

An installed module is found even when you run from inside the repository
folder. `./tools/build_accel.sh` builds it straight into the checkout instead.

## Decision record

> **Status note:** the rest of this page is the original design record for the fast path.
> The native kernel now executes strategies through `run_program` (Data2,
> sweeps, trace); the bars-only `run()` described below remains and always
> refuses. For current behaviour, read the sections above and `AGENTS.md`
> (`pl_transpiler.runtime.accel`).

This is the recorded decision on
the product shape of the optional native fast path, written **before** any kernel
code exists, so the boundary is agreed rather than discovered later. It changes no
behaviour. It promises no speed. This document is the contract; the kernel, CI, and
conformance work implements against it.

## The interface (stable — shared across the kernel, CI, and test code)

These three facts are the whole public interface of the fast path. Do not rename
them anywhere:

| Fact | Value |
|---|---|
| Extra name | `accel` — resolves once `powerbridge-accel` is on PyPI; until then install the wheel from the GitHub Release or build `accel/` (see [Install](#install)) |
| Native extension import path | `pl_transpiler._accel` |
| Extra absent | `import pl_transpiler._accel` raises `ImportError`; callers **fall back to the Python path loudly** and produce identical results |

These three names are shared verbatim by the kernel, the CI/wheel build and the
tests, and users' install commands depend on them, so we keep them stable:
renaming any of them is an interface break, not a cleanup.

The fallback rule in the third row is not decoration — it is the load-bearing
invariant, restated in the Invariants section of [AGENTS.md](../AGENTS.md) and in
[SECURITY.md](../SECURITY.md): **any construct the kernel cannot faithfully execute
must raise or fall back to Python loudly — it must never silently return a different
result.**

## What is *not* changing

- `pyproject.toml` `dependencies` stays `[]` — that is the product rule stated at
  the top of the file ("Pure-stdlib: the transpiler, emitter, runtime and gates
  import nothing off PyPI"). The transpiler, emitter, runtime, and gates import
  nothing off PyPI on the default path. `pip install .`
  remains a pure-standard-library, zero-dependency, no-build-step install, and that
  is the product.
- Without the extra, `pl_transpiler` behaves **bit-identically to today**: same
  Python path, same trades, same digests, same test tier. The extra is strictly
  additive.
- The default CI job never requires a Rust toolchain. Contributors without one keep
  running the whole public tier exactly as before.

## How installing the extra actually yields `pl_transpiler._accel` — the honest mechanism

Documenting the extra is the easy half. The hard half is *how `pip install .[accel]`
results in a usable native module at all*. Two mechanisms that look obvious are
ruled out by this repository's structure, and the conclusion is a product-level
decision this document deliberately does **not** make:

- **A build requirement cannot work.** The extra's dependencies are installed and
  *built by pip before* the project itself is built. A native kernel is the very
  thing the installed `pl_transpiler` must import at *run* time, so any build
  backend that tries to compile the kernel's output into *this* wheel at build
  time either deadlocks against pip's install ordering or lands the binary in
  the wrong artifact — the kernel's wheel. (A build-time-only toolchain in
  `[build-system] requires` — plain `cargo`, no `pl_transpiler` import — is how
  the single-distribution shape does compile, but see alternative 2 for why that
  shape is not chosen today.)
- **A runtime dependency on a pre-built kernel wheel is the mechanism — with a
  consequence that must be stated.** The dependency installs cleanly (it does
  not need `pl_transpiler` to import), and unpacking its wheel drops
  `pl_transpiler/_accel.*.so` into the installed `pl_transpiler` directory,
  where the core finds it as `pl_transpiler._accel`. What it means: the core
  and the kernel become **two distributions that jointly own one package
  namespace**, a familiar packaging pattern for optional native accelerators,
  and the convention it requires is that the kernel distribution's wheel metadata
  declares the shared files so the two installs do not fight over ownership of
  the same paths (and the kernel wheel must not restack files belonging to the
  core distribution).

So the real mechanism is this: **`pip install .[accel]` works only by declaring
one dependency that itself ships the native extension module
`pl_transpiler._accel`** — a second, separately built distribution whose wheel
installs that module into the `pl_transpiler` package directory. That distribution is
**`powerbridge-accel`**, built from `accel/` and published to PyPI alongside
`powerbridge` by `.github/workflows/release.yml`.

The rejected alternatives, and why — each is stated so nobody quietly ships a
broken install or a fake fast path:

1. **Point the extra at a git checkout as a dependency**
   (`accel = ["pl-transpiler-accel @ git+https://github.com/…"]`): pip's dependency
   resolver rejects direct VCS URLs in dependency specifiers — that syntax is only
   valid for requirements files and even there it is not installable from an index.
   This *cannot* be made to work in `pyproject.toml`; do not attempt it.
2. **Ship the Rust extension inside this same package via a custom
   `[build-system]` backend** (a maturin/PyO3-style backend that replaces the
   setuptools backend and shells out to `cargo`):
   it would make `pip install .[accel]` literally true on the source tree, but it
   couples every consumer's install to a Rust toolchain at build time and breaks
   the "core has no build step" promise for anyone whose wheel is not cached. It is
   a legitimate future shape, not today's decision.
3. **Make the extra a no-op or a source-tree-only marker** (declare it, depend on
   nothing): installing it would change nothing, and users would reasonably read
   `[accel]` as "the fast path is active" when it is not. This is the *silent wrong
   result* this project exists to refuse, in the install script itself. Rejected.

Because none of those is both true and achievable inside this repository alone, the
decision recorded here is the fourth option: **a second distribution, published
separately, whose wheel provides `pl_transpiler._accel`.** That distribution is
`powerbridge-accel`, pinned by the extra to the same version as `powerbridge`
(`accel = ["powerbridge-accel==<version>"]`), so `pip install powerbridge[accel]`
installs the matching kernel wheel where one is published for the platform.
`powerbridge-accel` is not on PyPI yet, so today the extra does not resolve:
install the release wheel by URL or build from source (see [Install](#install)). The
extra name `accel` stays stable so the kernel, CI/wheel build, conformance work
and tests share one name.

What is true *today*, verifiably, without the artifact:

```bash
pip install .            # works, zero dependencies, zero build step
python3 -c "import pl_transpiler._accel"   # -> ModuleNotFoundError
```

(`ModuleNotFoundError` — CPython's "no module named …" error from the default
importer — is a subclass of `ImportError` (verified on the 3.9 floor this project
supports), so the stable interface's `ImportError` is raised by every compliant
importer and caught by every compliant caller.)

That `ImportError` **is** the extra-absent behaviour from the stable interface, and
it is correct: callers must treat it as "run the Python path, and say so", never as
a failure to swallow.

## Trust model (summary; the normative text is SECURITY.md)

Today [SECURITY.md](../SECURITY.md) and [AGENTS.md](../AGENTS.md) tell you that
`pl_run` and `pl_oracle` **execute generated Python** on your machine. The accel
path is a **different trust surface**: a compiled kernel you cannot read, a binary
whose arithmetic is not reviewable in the way Python source is reviewable. Installing
the extra is trusting the wheel's publisher and its toolchain **in addition to**
trusting the strategy file. If you cannot audit that, you should not accelerate.

The equivalence bar: an `accel` run must produce the **exact same trade list** —
same fills, same order, same count — as the Python path (the `pl_oracle` trade-list digest),
not "close". A fast path that disagrees is a bug, not a feature, and the fail-loud
rule above is what turns disagreement into an error instead of a silent wrong answer.

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

## Verification tiers, accel

Mirroring the test-tier map in [AGENTS.md](../AGENTS.md): the public tier
(`python3 tests/run_public_tier.py`) exercises the Python path only and must stay
green with or without the extra — it never requires Rust. The differential
conformance harness and the full-tape fidelity gate are the gates that
keep the fast path honest; until they exist, the accel path, if built, must not be
trusted to run. Do not measure performance against the accel path while driving in
conditions it has never been proven to match; this document makes **no**
performance claims.

## Wheels and CI

The `accel` crate (`accel/` at the repo root) is a **separate distribution** whose
wheel installs `pl_transpiler._accel` next to the core package. The root
`pyproject.toml` `dependencies` list stays `[]`; the `accel` extra remains the
reserved binding point and is not required by anything.

- **Build (verified on a fresh arm64 macOS box, Apple M5 Max, Rust 1.98.1 /
  maturin 1.15.0):**

  ```bash
  cd accel
  maturin build --release
  # -> accel/target/wheels/powerbridge_accel-0.3.0-cp39-cp39-macosx_11_0_arm64.whl
  pip install ../target/wheels/powerbridge_accel-*.whl   # or pip install accel/target/wheels/*.whl from the root
  ```

  The wheel contains `pl_transpiler/_accel*.so` (the mixed-layout `python-source`
  in `accel/pyproject.toml` makes maturin place the cdylib at that path). Import
  from a cwd that is **not** the repo root, or the checkout's source `pl_transpiler`
  shadows the installed module (see `accel/README.md`).

- **What CI runs** (`.github/workflows/accel.yml`): two jobs.
  `accel-macos-arm64` **runs the accel path**, not merely builds it — it installs
  the extra, imports `pl_transpiler._accel` from a non-repo cwd, asserts
  `ping() == 42`, that `run_program` is present and that the bars-only `run()`
  refuses with `ACCEL_FALLBACK_REQUIRED`, then runs the public
  test tier and the differential conformance harness. That job **fails if the
  extra silently does not load**. `core-no-accel` installs core without the extra
  and proves the zero-dependency promise is itself tested: the import raises
  `ImportError`, and the public tier + conformance harness stay green.

- **Platform matrix.** macOS arm64 is the platform actually built and verified
  (this change's M5 Max evidence and the `accel-macos-arm64` CI job). Linux
  x86_64/aarch64 and Windows wheels are **clearly-marked follow-ups** — nothing
  in this change claims a platform that was not built. Do not add an unbuilt
  platform to CI as if it worked.

- **Kernel boundary.** The crate exposes `ping()` (returns 42) and the
  original bars-only `run()`, which carries no program and therefore always
  raises `NotImplementedError` with an `ACCEL_FALLBACK_REQUIRED` prefix.
  Strategies execute through the `run_program` family (program IR + series +
  config in, fills out; see [dev/ACCEL-INTERFACE-V2.md](dev/ACCEL-INTERFACE-V2.md)).
  The differential conformance harness compares the Python and accel paths when
  the extra is installed and skips cleanly when it is not; the wheel-load gate
  proves the installed extra loaded.
