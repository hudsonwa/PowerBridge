# Ground-truth captures — data card

## What these files are

Per-bar CSV output from running the ground-truth scripts in
[`ground_truth/`](..) in **MultiCharts**, the reference implementation the
transpiler is checked against. Each script writes one row per bar: a bar key
(`bn`, `date`, `time`) and one column per computed value (indicators, maths and
date functions, and for strategies the order, position and trade columns).

## What is and is not redistributed

- **Committed:** this file only.
- **Not redistributed:** the capture CSVs and the bar exports that produced them.
  They are MultiCharts output over licensed market data, and the folder is
  gitignored (`ground_truth/captures/*`, except this file).
- Without the captures, the capture-backed gates skip and the public tier stays
  green. To run them with your own captures, see
  [docs/CAPTURES.md](../../docs/CAPTURES.md).

## Files

Each label below maps to its EL source and capture file in
[`tests/gt_manifest.py`](../../tests/gt_manifest.py). The `sha256` column pins
the exact file the gates were last verified against;
`scripts/guard_ground_truth.py` checks every pinned file against it.

| file | label | EL source (under `ground_truth/`) | rows (data) | sha256 |
|---|---|---|---|---|
| `GT1_funcs.csv` | GT1 | `GT1_functions_indicators.txt` | 202,208 | `a0b4b6e534e825888f6e6e2452bdaf2980f0025092d1b3f656a42b3a13b7f5f0` |
| `GT2_strategy.csv` | GT2 | `GT2_strategy_orders_position.txt` | 202,179 | `ae6fa232c14c1c34a5b57504dad91d0f52295f270819951aec22d1aa25c80086` |
| `GT_master_m0_2.csv` | GT_master | `GT_master_strategy.txt` | 202,179 | `54565551aebf0ffc3f8b601490aff3e342ab8c36ab6673b58360e524bfa2787e` |
| `GTA5_o0_mm0_v3.csv` | GTA5 | `GTA5_strategy.txt` | 249,113 | `1b1fbfefdfc229c3b37d2bdb3d29d73e7283c49c6d5f9ae108409af9fbf6e159` |
| `GTA5_o1_mm0.csv` | GTA5_o1 | `matrix/GTA5_o1_mm0.txt` | 202,179 | `577fa4e06051e251af9207280d2896f3f81a2ae6bdcbeb30c33e4bf0063feb8d` |
| `GTA5_o2_mm0.csv` | GTA5_o2 | `matrix/GTA5_o2_mm0.txt` | 202,179 | `e24e2ecead1a067236be51ef3801277e198c57aa1890300937349ef9245fface` |
| `GTA5_o3_mm0.csv` | GTA5_o3 | `matrix/GTA5_o3_mm0.txt` | 202,179 | `a5f866d9ffe55f81b79734d2435699797279c5dc5ff34f09eb1fe4a670d6fa12` |
| `GTA5_o4_mm0.csv` | GTA5_o4 | `matrix/GTA5_o4_mm0.txt` | 202,179 | `01d099370feaed436a8e5be088780c3110d6d3e0ebc0f523543f8c17f8cc417d` |
| `GTA6_o0_mm0.csv` | GTA6_1 | `GTA6_strategy.txt` (file 1) | 249,159 | `7affda49b0df23a84222743096ef8dd38ef1d4cd8a883bccbdd0d84562fb6a80` |
| `GTA6_o0_mm0_b.csv` | GTA6_2 | `GTA6_strategy.txt` (file 2) | 249,159 | `6c9655d81968c8c52f9f63b1bed1fe8a7e3b8e6655530bc05e702f8319ae7116` |

`GT3_coverage.csv` and `GT4_coverage.csv` (labels GT3 and GT4) are also listed in
`tests/gt_manifest.py` but are not pinned here.

Notes:

- GTA5_o1 to GTA5_o4 run the same script as GTA5 with `OrderMode` 1 to 4 (limit
  entries, stop entries, this-bar-on-close, named exits from entry).
- GTA6 writes two files because of a column limit: file 1 holds calculation, data,
  array, date, session and bar-metadata columns; file 2 holds strategy statistics,
  trade introspection and entry/exit columns.
- Indicator and strategy scripts start at different warm-up bars, so row counts
  differ between files over the same bars. Align on `bn` or `date` + `time`, never on
  row index.

## How the tests use them

- `tests/run_gt.py` runs a label's EL source through the engine, and
  `tests/golden_diff.py` compares each column with the capture at a float32
  tolerance (`rtol = atol = 1e-6`).
- `tests/verify_all.py` sets the number of columns each label must match;
  `tests/verify_full_range.py`, `tests/verify_roundtrip.py` and
  `tests/verify_twoway.py` run the full-range, round-trip and two-way gates.
- `tests/test_failloud.py`, `tests/test_general_entrypoint.py` and other
  capture-backed tests use `tests/capture_gate.py` to skip when the files are absent.
- `python tools/analyze_capture.py <capture.csv>` streams one capture and reports its
  trade count and column health without loading it into memory.

## Reproduce or refresh

Run the scripts in [`ground_truth/`](..) in MultiCharts on the same symbol, interval
and date range (see [docs/CAPTURES.md](../../docs/CAPTURES.md)). Delete any old CSV
first, because `Print` appends. Put the files here, then check that each `sha256`
matches, or update this table if the dataset changed on purpose.
