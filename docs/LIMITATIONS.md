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

# Limitations

What PowerBridge does not do yet, and where its validation stops. What the
engine assumes about commission, slippage and position size is in the
[usage guide](USAGE.md#what-the-engine-assumes).

## Unsupported keywords

Default is all or nothing. `pl_transpile` walks the whole file, lists every
unimplemented keyword with its line, writes no Python, and exits non-zero.

`--partial` is only for incremental porting. Stubs raise when they run. Do not
trust a backtest from partial output.

## Known gaps

- The Python to EL direction only reads the **mirror dialect**: the clean Python
  this transpiler itself emits. Arbitrary hand-written Python is out of scope.
- Validated against MultiCharts on **1-minute @NQ** and **1-minute @ES**
  (@ES#C RTH, 2016: four configurations of a real-world multi-entry strategy,
  4,278 of 4,278 fills identical). The MultiCharts captures behind these
  numbers are licensed and not in this repository; see
  [what you can verify](verification.md#what-you-can-verify-from-this-repo).
  Other instruments and timeframes are untested.
- Some builtins are not implemented yet. A run that calls one **stops**
  with a one-line error naming the builtin and the first source line that
  uses it, rather than guess a value. To run anyway, pass `--lenient` to
  `pl_run` / `pl_oracle` or set `PL_STRICT=0`: those builtins then return
  0/'', the run ends with a `WARNING` naming them, and
  `run_el(...)["unimplemented"]` lists them. Cosmetic ones (drawing, sound,
  print) never stop a run and are not reported.
- **MaxBarsBack is opt-in, so warm-up fills can differ from MultiCharts.**
  By default strategies are evaluated from bar 1, and a windowed function such
  as `Average(Close, 10)` returns 0 until it has 10 bars; MultiCharts does not
  evaluate its first MaxBarsBack bars at all. When a strategy may read past bars
  (`Close[n]`, `Close of n bars ago`, `TrueRange`, a windowed builtin such
  as `Average` or an `*FC` variant, or a user function) and `--max-bars-back`
  is not set, `pl_run` and `pl_oracle` print one `note:` line on stderr
  saying so, unless they already print the `lookback past available history`
  warning (`-q` hides only the note; the library API never prints it). See "Matching MultiCharts" below.
- Per-bar MultiCharts parity captures are not shipped. Those tests skip on a
  fresh clone.
- The `accel` fast path is optional: install the prebuilt wheel from a
  release (macOS, Linux and Windows) or build it from source; see
  [ACCEL.md](ACCEL.md). It does not yet
  run strategies that declare **arrays**; those fall back to Python, loudly.
  `pl_run` (per-bar trace rows) uses it only for full-tape runs without
  `--max-bars-back`, `--max-entries-per-direction` or `--partial`; other runs
  use the Python path and say so on stderr.
- Same-direction entries always add to the position (as with MultiCharts'
  'allow multiple entries' setting). MultiCharts' default setting refuses
  them; that default is documented behaviour, not verified against a capture.
  Pass `--max-entries-per-direction 1` to get it.
- `Sell` / `BuyToCover ... from entry("A")` that would fill while more than
  one entry is open in that direction **stops** the run (both engines).
  EasyLanguage documents it as closing only entry A, but no MultiCharts capture
  confirms the fills ([#24](https://github.com/hudsonwa/PowerBridge/issues/24)).
  `PL_STRICT=0` / `--lenient` keeps the old behaviour (the whole position
  closes, on the Python engine) with a `WARNING` naming `from entry`. With one entry open,
  a named exit fills as before.
- Fail-loud assumes valid EasyLanguage. A residual gap: assigning to a
  read-only report keyword that is not in the bundled catalog can be treated as
  a local. Catalog-known names are still caught.
- **Matching MultiCharts:** MultiCharts skips the first MaxBarsBack bars
  (default 50); pass `--max-bars-back 50` to reproduce that. PowerBridge's
  default is 0: every bar is evaluated, and a `Close[n]` read with too little
  history returns that bar's Open (one stderr warning per run says so).
  Only literal lookbacks are checked for that warning; `Close[Length]` is not.
  To refuse repeat same-direction entries as MultiCharts' default setting
  does, pass `--max-entries-per-direction 1`.
- This tree is the library, tests, and docs. Internal build tooling is not
  shipped.
