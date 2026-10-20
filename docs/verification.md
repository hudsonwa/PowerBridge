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


# Verification (maintainers)

How PowerBridge proves a change did not alter trading results. New users only
need the [README Quickstart](../README.md#quickstart).

## The trade digest is the merge gate

```bash
pl_oracle ground_truth/GT2_strategy_orders_position.txt examples/NQ_sample_bars.csv
```

`pl_oracle` prints an exact trade-list digest on stdout. Two runs on unchanged
code must be byte-identical, and any performance change must leave the digest
unchanged.

`pl_oracle` uses the Rust kernel automatically when the `accel` module is
installed, and says so on stderr (`[native]`). `--python` forces the reference
Python path; the sha256 must match. Whenever a run ends up on the Python path,
a one-line `note:` on stderr says why. Stdout and the digest never change.

Before merging engine, codegen, parser or runtime work, compare native and
`--python` digests on every file in `examples/` and `ground_truth/GT2*`.

## Date windowing

`pl_run` and `pl_oracle` accept an opt-in `--from-date` / `--from-bar` window.
Default is still the full tape. `pl_run` seeds the discarded prefix (state and
lookback stay primed, fills suppressed) unless you pass `--slice`. `pl_oracle
--from-date` stays a tape slice; pass `--seed-warmup` to seed instead. A
windowed run that disagrees on trades is a failed ticket, not a speed win.

## Private captures

Per-bar MultiCharts parity captures are licensed data and are not shipped.
Tests that need them skip cleanly on a fresh clone. See
[CAPTURES.md](CAPTURES.md) to attach your own.
