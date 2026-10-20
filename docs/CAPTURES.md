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

# Bring-your-own captures & bars

The transpiler is validated against **per-bar ground-truth captures**: CSV files
produced by running the reference EasyLanguage/PowerLanguage scripts in
**MultiCharts** (the authoritative oracle) and recording every computed column on
every bar. The capture-backed gates then re-run the same scripts through this
project's engine and require a byte/​float32-exact per-bar match against the
capture.

Those capture CSVs are large and are **not shipped** with this project — only the
checksummed manifest (`ground_truth/captures/MANIFEST.md`) is. Everything you need
to run the portable test tier works **without** them:

```bash
python3 tests/run_public_tier.py
```

The capture-dependent gates simply **skip** (with a one-line pointer back to this
document) until you attach your own captures. This page explains how to do that.

---

## 1. What a capture file looks like

A capture is a plain CSV with a header row and one row per bar:

- A **bar key** column: `bn` (BarNumber), plus `date` (EL `YYYMMDD`) and `time`
  (`HHmm`/`HHmmss`). Diffs are aligned on the bar key, **never on row index** —
  indicator and signal scripts can start at different warmup points.
- One column per **computed quantity** the script emitted: indicator values, math
  and datetime results, and — for strategy scripts — the order/position/trade
  columns (`marketpos`, `curcontracts`, `entryprice`, `netprofit`, `totaltrades`,
  …).

Each script/label maps to a `(source .txt, capture .csv)` pair; that mapping is the
single canonical dict in `tests/gt_manifest.py`. The comparison itself is done by
`tests/golden_diff.py` with a float32 tolerance (`rtol = atol = 1e-6`) so genuine
single-precision accumulation differences do not count as mismatches.

## 2. Where the files go

Drop each capture CSV into:

```
ground_truth/captures/<name>.csv
```

using the **exact filename** listed in `ground_truth/captures/MANIFEST.md`. The
capture-gate probe (`tests/capture_gate.py`) considers the captures "attached" only
when every filename pinned in the manifest is present under that directory; if any
are missing the gates skip rather than fail.

## 3. Verify the checksums

`ground_truth/captures/MANIFEST.md` pins the `sha256` (and row count) of every
capture. After copying a file in, confirm it matches:

```text
shasum -a 256 ground_truth/captures/<name>.csv
```

Compare the printed digest against the `sha256` column in the manifest. A mismatch
means the dataset differs from the one the gates were pinned against — regenerate
from the same symbol/interval/range, or update the manifest if you intentionally
changed the dataset.

## 4. Run the capture-backed gates

Once the pinned captures are present, the full suite runs them automatically:

```text
# Full regression gate (GT parity + reverse round-trip/two-way gates).
python3 tests/verify_all.py            # add --fast for a quick calc-window pass

# The individual capture-backed gates also run standalone:
python3 tests/verify_roundtrip.py
python3 tests/verify_twoway.py
python3 tests/verify_full_range.py
```

Each of these prints the same one-line skip notice (pointing here) if the captures
are absent, and otherwise runs exactly as it does in the full checkout.

## 5. Bring your own bars (no captures needed)

To run *arbitrary* transpiled EL over *your own* OHLCV bars — independent of the
pinned captures — use the general entrypoint `pl_run`:

```text
python3 -m pl_transpiler.tools.pl_run <strategy.txt> <bars.csv> --config NQ -o out.csv
```

`bars.csv` is a plain OHLCV file with the header:

```
date,time,open,high,low,close,volume
```

(`date` as EL `YYYMMDD`, `time` as `HHmm`). The same engine and fill logic that the
capture gates exercise drives this path, so it is a faithful way to run strategies
on your own data. As a library:

```text
from pl_transpiler.tools.pl_run import run_el, load_bars_csv
from pl_transpiler.runtime.instrument_config import get_config

el_source = open("strategy.txt").read()
bars = load_bars_csv("bars.csv")
out = run_el(el_source, bars, get_config("NQ"), trace_columns=["marketpos", "netprofit"])
# out = {"columns": [...], "rows": [[...], ...], "trades": [...]}
```

## 6. Producing captures yourself

To create captures the gates can check against, run the reference scripts in
MultiCharts on the same instrument/interval/range and print each computed column
per bar to a CSV (delete any existing file first — the print appends). Match the
`(source .txt, capture .csv)` names in `tests/gt_manifest.py`, drop the CSVs under
`ground_truth/captures/`, and record their `sha256` + row count in the manifest.
