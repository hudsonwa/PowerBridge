# `powerbridge-drawing-dump` v1

The format of a drawing dump. `docs/DRAWING_DUMP.md` describes how to produce
one; this file is the short contract.

- JSON Schema: `schema/drawing-dump.v1.json` (draft 2020-12). Cross-field
  rules are checked by `pl_transpiler.runtime.drawing_dump.validate`, which
  needs no `jsonschema` package.
- Canonical bytes: UTF-8, sorted keys, separators `(",", ":")`, one trailing
  LF. Equal content gives an equal sha256.
- Bar key: `bn` (1-based index into the tape, not MultiCharts `CurrentBar`),
  EL `date` (YYYMMDD), `time` (HHmm), `time_s` (HHmmss).
- An anchor need not be a bar on the tape (a v1 extension): `(0, 0)` from
  an out-of-history lookback, or a session-end time, is kept as given with
  `bn` null. `bn` is looked up on the whole tape, so an anchor on a later bar
  has its `bn`. This is PowerBridge's own convention; it has not been
  checked against MultiCharts. Only a null-`bn` anchor may have a date or
  time outside the EL ranges, and its `(date, time)` must not be a tape bar.
- Prices: `price` plus `price_ticks = round(price / tick)`. Bar-price anchors
  are compared on ticks.
- Colours: MultiCharts integer `R + 256·G + 65536·B` (Red = 255). `argb` is
  derived for humans and never compared on its own.
- Objects are matched by `seq` (creation order). `id` is compared only when a
  case sets `compare_ids`.
- `final_count` equals the number of objects whose `deleted` is null.

Versioning: a new optional field needs no bump. A rename, removal or change
of meaning bumps `version`.

Comparison (`tests/_drawing_diff.py`): counts, seq, type, bar keys,
`price_ticks`, ints and bools are exact; text and font are exact bytes;
computed values use the float32 rule `|a-b| <= 1e-6 + 1e-6·|b|`. An unknown
schema or version, a tape sha mismatch, or a missing or extra object fails
with a named reason.
