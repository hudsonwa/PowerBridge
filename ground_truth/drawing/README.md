# Drawing cases

Each `g-*` folder is one drawing case:

- `source.el` — the EasyLanguage study.
- `case.json` — what the case covers (`behaviour`, `keywords`), whether ids
  are compared (`compare_ids`), whether it needs a Data2 series (`data2`),
  and any known failure (`xfail`).

The tests run every case on a small synthetic bar series, so the recorder,
ids, anchors and schema are checked in any clone.

The MultiCharts readbacks these cases were checked against are not in the
repository: they are built from licensed market data, which cannot be
redistributed. If you have your own copy, put it next to the cases (both
paths are git-ignored):

- `tapes/data1.csv.gz` — the Data1 bars as a PowerBridge CSV.
- `tapes/data2.csv.gz` — the Data2 bars as a PowerBridge CSV (for cases
  with `"data2": true`).
- `tapes/SHA256SUMS` — optional; `sha256  path` lines for files under
  `tapes/` and `g-*/mc/`, paths relative to this folder.
- `g-*/mc/drawings.json` — the MultiCharts dump for the case.
- `g-*/mc/trades.digest` — the trade digest of the same run.

`tests/test_mc_drawing_corpus.py` and the corpus tests in
`tests/test_drawing_dump.py` then compare PowerBridge against those files.
Without them, those tests skip. See `docs/DRAWING_DUMP.md` and `SCHEMA.md`.
