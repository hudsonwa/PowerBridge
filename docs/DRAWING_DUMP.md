# `powerbridge-drawing-dump` v1

Contract for a drawing dump produced by `run_el` / `pl_oracle` when drawing
capture is turned on, and for every drawing dump in
`ground_truth/drawing/` (read back from MultiCharts 16).

- JSON Schema: `ground_truth/drawing/schema/drawing-dump.v1.json`.
  The validator is `pl_transpiler.runtime.drawing_dump` (no `jsonschema`
  dependency). A failure is a `DumpError` naming the path.
- Canonical bytes: UTF-8, sorted keys, separators `(",", ":")`, one trailing
  LF. `pl_oracle --drawings-json FILE` writes those bytes.
- Bar key: `bn` (1-based index into the tape passed to the run, not
  MultiCharts `CurrentBar`), EL `date` (YYYMMDD), `time` (HHmm), `time_s`
  (HHmmss).
- An anchor need not be a bar on the tape (a v1 extension): `(0, 0)` from
  an out-of-history lookback, or a session-end time, is kept as given with
  `bn` null. `bn` is looked up on the whole tape, so an anchor on a later bar
  has its `bn`. This is PowerBridge's own convention; it has not been
  checked against MultiCharts. Only a null-`bn` anchor may have a date or
  time outside the EL ranges, and its `(date, time)` must not be a tape bar.
- Prices: `price` plus `price_ticks = round(price / tick)`. Comparisons use
  ticks.
- Colours: MultiCharts integer `R + 256·G + 65536·B` (Red = 255). `argb` is
  derived for humans and is not a separate comparison.
- Objects are matched by `seq` (creation order). `id` is compared only when
  a case sets `compare_ids`.
- `final_count` is the number of objects whose `deleted` is null, by type.

Capture is opt-in. `run_oracle(..., drawings=True, drawing_case_id=...,
drawing_tape={"name", "sha256"})` adds a `drawings` key. Without it the
digest text and the `run_el` return keys are unchanged. The tape sha is the
caller's to choose; two dumps compare only when their tape shas agree. The
corpus tests take the name and sha from each case's `mc/drawings.json`.

The MultiCharts readbacks and the bar data behind them are licensed and are
not in the repository; `ground_truth/drawing/README.md` says where to put
your own copy. Without it the corpus tests skip and the rest run on
synthetic bars.

`use_native=True` records the same objects in the native kernel.
`producer.kind` is then `pb-native` instead of `pb-python`; that is the only
difference in the envelope, since both paths build the dump with the same
`build_dump` helper. A drawing or colour call the kernel does not implement
is refused, and `run_oracle` falls back to Python. The kernel never skips a
prefix of a program that draws, because drawing calls are side effects even
on bars that cannot trade.

Comparison (`tests/_drawing_diff.py`): counts, seq, type, bar keys,
`price_ticks`, ints, bools and text are exact. Computed values use the
float32 rule `|a-b| <= 1e-6 + 1e-6·|b|`. An unknown schema or version, a
tape sha mismatch, or a missing or extra object fails closed.

Creation defaults are the measured MultiCharts values: a trendline is colour
36095, style 1, size 0; an up arrow is 16711680 and a down arrow is 255,
style 10, size 1; text is colour 7058764, horizontal and vertical style 2,
font size 9, background 0, border false, font ``?`` (the readback does not report
the real face).
