# Ground-truth captures — manifest

Per-bar CSV output produced by running the GT scripts in **MultiCharts** (the
authoritative oracle for the transpiler). The CSV files themselves are large and
**gitignored** (`ground_truth/captures/*.csv`); this manifest is committed so the
captures are documented, checksummed, and reproducible.

## Provenance

- **Source:** MultiCharts runs of the shipped GT scripts, produced by the
  project owner.
- **Retrieved:** 2026-06-17.
- **Rename note:** the owner renamed the GT_master OrderMode-0 capture from
  `GT_master_m0.csv` to **`GT_master_m0_2.csv`**. It is the **OrderMode 0**
  (MA-cross reversal, next bar at market) run. OrderModes 1–4 not yet captured.
- **Compile status:** all three scripts compiled and ran in MultiCharts — the
  captures are the proof (GT_master, GT1, and GT2 all produced full output).

## Dataset

- Instrument: ~18,900 price level, 1-minute bars — appears to be **NQ / Nasdaq-100
  futures** (confirm exact symbol/contract).
- Span (EL `Date` YYYMMDD): `1240102` → `1251231` = **2024-01-02 → 2025-12-31** (~2 years).
- The exact bar series fed to the transpiler's Python side MUST match this capture
  bar-for-bar (export the same series from MultiCharts) or per-bar diffs will be spurious.

## Files

| file | rows (data) | bytes | sha256 |
|---|---|---|---|
| `GT1_funcs.csv` | 202,208 | 85,674,978 | `a0b4b6e534e825888f6e6e2452bdaf2980f0025092d1b3f656a42b3a13b7f5f0` |
| `GT2_strategy.csv` | 202,179 | 18,520,944 | `ae6fa232c14c1c34a5b57504dad91d0f52295f270819951aec22d1aa25c80086` |
| `GT_master_m0_2.csv` | 202,179 | 49,069,012 | `54565551aebf0ffc3f8b601490aff3e342ab8c36ab6673b58360e524bfa2787e` |
| **`GTA5_o0_mm0_v3.csv`** ✅ CANONICAL | 249,113 | 197,343,736 | `1b1fbfefdfc229c3b37d2bdb3d29d73e7283c49c6d5f9ae108409af9fbf6e159` |
| ~~`GTA5_o0_mm0.csv`~~ superseded (v1, session-mismatch) | 849,961 | 666,435,817 | `606365af22f226bd2a187fa6f9dba5bb977b05b7774128218ba3ed9bcb9ecfc0` |
| **`GTA6_o0_mm0.csv`** ✅ (file1, 85 cols) | 249,159 | 143,395,934 | `7affda49b0df23a84222743096ef8dd38ef1d4cd8a883bccbdd0d84562fb6a80` |
| **`GTA6_o0_mm0_b.csv`** ✅ (file2, 74 cols) | 249,159 | 116,086,916 | `6c9655d81968c8c52f9f63b1bed1fe8a7e3b8e6655530bc05e702f8319ae7116` |
| | | | |
| _GTA6 capture 2026-06-19 (OrderMode 0, MMVariant 0). Compiled first-try after the SymCcy type fix; headers EXACT-match the script; cotval=1.0 (Cotangent 45°), times=HHmmss, symccy numeric. Span 2024-01-02→2026-06-18._ | | | |

**Row-count note:** GT1 (indicator) has 29 more rows than GT2/GT_master (signals),
and its first bar is earlier in the session (time 852 vs 921). This is expected —
the indicator and the signals have different warmup / MaxBarsBack start points.
Align on `BarNumber`/`date`+`time` when diffing, not on row index.

## Validation (ground truth confirmed sane, 2026-06-17)

Checked across all ~202k bars:
- GT1 constant probes hold exactly: `powr=8` (Power(2,3)), `exp=2.718282` (ExpValue(1)),
  `strlen=6`, `instr=3`, `lefts=abc`, `rights=ef`, `uppers=ABC`.
- GT1 persistence/history: `counter` increments 1→202208; `prevcounter == counter-1` every bar.
- GT_master loops: `whilesum=10`, `evensum=6` constant (For + While correct).
- GT_master `once`: `onceinit` frozen at the first bar's close (18902.25) for all bars.

## GTA5 capture (OrderMode 0, MMVariant 0) — retrieved 2026-06-19

The re-grounded GTA5 signal (compile-fixed: `SetDollarTrailing_pt` → `SetTrailingStop_pt`,
commit `b6b8701`) **compiled, ran, and traded** in MultiCharts. Verified with
`python tools/analyze_capture.py` (a streaming O(1)-memory reader for these large files):

- **Traded: yes — MC `totaltrades` = 14,212** (its own authoritative count). Per-bar (clean)
  signal counts: LE_MKT 4,787 / SE_MKT 4,786; 10,174 position flips; final netprofit −71,410
  (a coverage strategy, not a profitable one — expected). All 90 columns present; Data2 100%
  populated. **The earlier "no trades" was the slow per-bar `Print(File())` write, not a bug.**
- **Dataset DIFFERS from the GT captures:** span `1240102 → 1260618` = **2024-01-02 → 2026-06-18**,
  **249,113 bars** (vs the GTs' 202,208 bars ending 2025-12-31). Phase-4 must feed the transpiler
  the matching 249,113-bar series exported from MC, not the GT 202k series, or per-bar diffs are spurious.
- **Raw file has ~3.41× rows/bar (849,961 rows / 249,113 bars) — a DATA1/DATA2 SESSION-TEMPLATE MISMATCH,
  not intrabar calc.** Re-running gives byte-identical output (deterministic). Bar Magnifier is OFF and IOG
  is off (confirmed by the owner — Strategy Properties → Backtesting). Root cause, proven from the data:
  across the ~976 duplicate rows of a session-close bar, **only the Data2 columns move** (`d2close/d2c/d2h/
  d2l` take ~355 distinct values, sweeping the overnight) while Data1's `open/high/low/close/time` stay
  frozen. Data1 (@NQ#C) is on a **day session that ends 15:15**; Data2 runs a **longer (Globex/24h) session**.
  MultiCharts evaluates the strategy on every Data2 bar, so during Data1's off-session every Data2 bar
  attaches to Data1's frozen last bar → ~976 prints (≈ overnight minutes). Confirmed by the pattern: dups
  sit only at Data1 session-close times (15:15, + 12:15 holiday early-closes) and balloon around market
  holidays (2025-01-08 before the Jan-9 day of mourning → 1,906; July-3 pre-July-4 → 1,351).
- **RESOLVED (2026-06-19): `GTA5_o0_mm0_v3.csv` is the corrected capture** — the owner aligned Data1/Data2
  sessions and re-ran. It is natively clean: **249,113 rows = 249,113 bars (1.0×, no duplication)**, 90
  columns, Data2 100% populated, `totaltrades`=14,212 (trade logic unchanged). **Use v3 for Phase-4 diffing.**
  The fix was: Format each data series → Settings → Sessions → set Data1 and Data2 to the SAME template,
  delete the CSV (`Print` appends), re-run.
- **Why the earlier dedup was retired:** diffing the old keep-last dedup vs v3 showed **exactly 617 bars
  differ** (the session-close bars) — not only in the Data2 columns but also in position/state columns
  (`marketpos`, `totaltrades`, `entryprice`, `counter`, …), because keep-last grabbed the overnight-end
  Data2 row after the close-exit had already settled. So the dedup was wrong on those 617 bars; it has been
  deleted. v1 (session-mismatched raw) is superseded and removed locally (re-downloadable via its Drive id).

## GTA5 INPUT DATA (the bar series to feed the Phase-4 transpiler) — verified 2026-06-19

MultiCharts ASCII exports of the two series that produced `GTA5_o0_mm0_v3.csv`. Format:
`<Date>, <Time>, <Open>, <High>, <Low>, <Close>, <Volume>` (Date = `D/MM/YYYY`, 1-minute bars).
Gitignored (`captures/*.txt`); large, local-only.

| file | role | rows | span | sha256 |
|---|---|---|---|---|
| `@NQ#C 1 Minute.txt` | **Data1** (primary) | 249,163 | `2/01/2024 08:31` → `18/06/2026 14:29` | `ed2124012d26b80f9bd744cacf24c5e557d6d853f1b1a872dd971fa3ef4b84c4` |
| `@NQ# 1 Minute.txt` | **Data2** (secondary) | 1,621,752 | `7/06/2010 08:31` → `18/06/2026 15:15` | `2cd1536f5e47819ed11f666e1c9269821e6bbd1832e711bab178f809ddce8e9f` |

**Deterministically verified (streaming, no load-into-memory):**
- Both files clean: 0 malformed rows, 0 OHLC violations (`H≥L`, `H≥max(O,C)`, `L≤min(O,C)`),
  strictly monotonic timestamps, 0 duplicate timestamps.
- **Data1 ↔ v3 alignment exact:** Data1 has 249,163 rows = v3's **249,113 bars + 50 MaxBarsBack warmup**
  (08:31→09:20 = 50 bars; capture `bn=1` is at 09:21 = raw row 51, so **MaxBarsBack = 50**). The first
  captured bar (2024-01-02 09:21) and last (2026-06-18 14:29) match the raw @NQ#C OHLCV byte-for-byte,
  and the capture's `d2close/d2h/d2l` match the raw @NQ# bars at those timestamps.
- **Phase-4 runner contract:** feed ALL 249,163 @NQ#C bars as Data1 (the first 50 are warmup — emit no
  comparison output until raw row 51 / capture `bn=1`), and @NQ# as Data2 (it covers Data1's range with
  years to spare; align Data2 to Data1 by `date`+`time`). Diff the runner's per-bar output against
  `GTA5_o0_mm0_v3.csv` on `bn`/`date`+`time`, never row index.

## GTA5 OrderMode 1–4 captures (limit/stop/on-close/named-exit fills) — retrieved 2026-06-29

Four fresh GTA5 captures closing the order-mode coverage gap (1/4 → 4/4). Each was run in ONE MC
session on a fresh 2-year NQ chart (Data1 `@NQ#C` back-adjusted, Data2 `@NQ#` unadjusted),
1-contract base, commission/slippage 0, Bar Magnifier OFF, IOG OFF. 202,179 capture rows each
(= 202,229 data bars − 50 MaxBarsBack warmup). Span 2024-01-02 09:21 → 2025-12-31 15:15.

**Verified matched pair (2026-06-29, by timestamp, not assumed):** capture `bn=1` (1240102/0921)
Data1 OHLC `18900.25/18907.50/18897.75/18902.25` == fresh `@NQ#C` @ 09:21; `d2close=16738.25` ==
fresh `@NQ#` @ 09:21. The fresh data files are **byte-identical to the repo `@NQ#C`/`@NQ#` over the
2024–2025 overlap** (fresh `@NQ#C` md5 == `head -202230` of repo `@NQ#C`; fresh `@NQ#` == repo `@NQ#`
by timestamp) — i.e. the repo series restricted to 2024–2025, NOT a re-adjusted set. Landed under
distinct names so they never overwrite the o0/GTA6 data and each capture validates against its own bars.

| file | role | rows | sha256 |
|---|---|---|---|
| `GTA5_o1_mm0.csv` | OrderMode 1 — **limit entries** | 202,179 | `577fa4e06051e251af9207280d2896f3f81a2ae6bdcbeb30c33e4bf0063feb8d` |
| `GTA5_o2_mm0.csv` | OrderMode 2 — **stop entries** | 202,179 | `e24e2ecead1a067236be51ef3801277e198c57aa1890300937349ef9245fface` |
| `GTA5_o3_mm0.csv` | OrderMode 3 — **this-bar on close** | 202,179 | `a5f866d9ffe55f81b79734d2435699797279c5dc5ff34f09eb1fe4a670d6fa12` |
| `GTA5_o4_mm0.csv` | OrderMode 4 — **named exits from entry** | 202,179 | `01d099370feaed436a8e5be088780c3110d6d3e0ebc0f523543f8c17f8cc417d` |

Paired bar data (provenance; not guard-enforced — guard pins `.csv` only):

| file | role | rows | span | sha256 |
|---|---|---|---|---|
| `@NQ#C 1 Minute gta5_1to4.txt` | Data1 (adj, o1–o4) | 202,229 | `2/01/2024 08:31` → `31/12/2025 15:15` | `2ce2368cddeba2ee3d7af252da9d53f992d9ac1c6cbc2ffbaf49726558856098` |
| `@NQ# 1 Minute gta5_1to4.txt` | Data2 (unadj, o1–o4) | 202,229 | `2/01/2024 08:31` → `31/12/2025 15:15` | `66450d65eaa0811dd18327b081f7562ad80c913ef4b8efe2355a27a2fcdcdc80` |

## Reproduce / refresh

Re-run the scripts in MultiCharts per [`../README.md`](../README.md) and
[`../GROUNDING_RECIPE.md`](../GROUNDING_RECIPE.md) on the same symbol/interval/range,
delete the old CSV first (Print appends), then drop the files here. Verify the sha256
matches (or update this manifest if the dataset intentionally changed).
