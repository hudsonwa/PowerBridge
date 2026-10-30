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

## What you can verify from this repo

Everything below runs on a fresh clone with only the shipped source and the
synthetic sample bars in `examples/`. No captures and no network are needed.
Set up first as described in
[CONTRIBUTING.md](../CONTRIBUTING.md#set-up-a-development-environment).

| Check | Command |
| --- | --- |
| Full unit and conformance suite (includes the public tier) | `pytest -q --timeout=60` |
| Public test tier only (parser, codegen, runtime, oracle, mutation pre-flight) | `pytest -m public_tier`, or without pytest `python3 tests/run_public_tier.py` |
| Lint | `ruff check .` |
| Native kernel (only if you built it) | `cd accel && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --release` |
| Native vs Python digest | see below |

Native vs Python digest: the Rust kernel and the reference Python path must
give the same trade-list sha256 on every example and `GT2` program:

```bash
for f in examples/*.el ground_truth/GT2*; do
  python3 -m pl_transpiler.tools.pl_oracle "$f" examples/NQ_sample_bars.csv | grep sha256
  python3 -m pl_transpiler.tools.pl_oracle --python "$f" examples/NQ_sample_bars.csv | grep sha256
done
```

Each pair of lines must match. Without the kernel installed both runs use the
Python path; build it with `./tools/build_accel.sh` to compare the two (see
[ACCEL.md](ACCEL.md)).
`tests/test_accel_conformance.py` runs the same differential check over
`tests/conformance_corpus.json` and skips with a message when the kernel is
not built.

A clean public tier shows only skips for checks that need MultiCharts
captures, and each skip names the missing input. Full `pytest` leaves the
native-kernel tests out (they are not collected) until you build the kernel;
one summary line says how many. It also skips
the reference audit-tape tests, which need a tape that is not shipped (set
`PB_AUDIT_EL`, `PB_AUDIT_BARS` and `PB_AUDIT_D2` to run them).

**What you cannot verify from this repo.** MultiCharts parity (the per-bar
captures) is checked against
MultiCharts output that is licensed and cannot be redistributed. Those
captures are not in this repository, and the tests that use them skip here.
To check parity yourself, export your own captures from MultiCharts and
attach them as described in [CAPTURES.md](CAPTURES.md).

## The trade digest is the merge gate

```bash
pl_oracle ground_truth/GT2_strategy_orders_position.txt examples/NQ_sample_bars.csv
```

`pl_oracle` prints an exact trade-list digest on stdout. Two runs on unchanged
code must be byte-identical, and any performance change must leave the digest
unchanged.

The fill log has one record per contract for every change of the signed
position: an entry, an exit, both legs of a reversal (the close leg, then the
open leg, at the same price and time), and exits from `SetStopLoss`,
`SetProfitTarget` and the other Set* risk stops (their signal is the stop's
name, e.g. `stop_loss`, `profit_target`, `trailing_stop`; SetExitOnClose logs
`End of Day Exit`). `round_turns` is the number of exit records, so it counts
per contract (as v1 did); `closed_positions` gives a per-position count.

Each hashed fill line starts with the order's buy/sell side, not a position
label: `EntryLong` means a buy-side fill (Buy, BuyToCover, or a risk exit of a
short) and `ExitLong` a sell-side fill (Sell, SellShort, or a risk exit of a
long), so a SellShort from flat hashes as `ExitLong`. This field is fixed
because every pinned digest depends on it.
For a readable fill log with the real position effect, add `--fills-csv FILE`:
its `trade_type` column is `EntryLong`, `ExitLong`, `EntryShort` or
`ExitShort`, replayed from the position before and after each fill (a reversal
becomes exit rows followed by entry rows). It is a new file and does not change
the digest.

### Digest versions

The first line of the digest names its version. The current version is **v2**:
the hashed text starts with the line `digest_version: 2`, followed by one
canonical line per fill. v1 digests (PowerBridge 0.2.0) were computed from an
incomplete fill log: a reversal logged only the contracts by which the
position size changed (so long 1 -> short 1 logged nothing), Set* risk exits
were not logged, and `round_turns` was `fills // 2`. The engine's position and
PnL were not affected, only the log.

A v1 digest and a v2 digest of the same run never compare equal. If you pinned
v1 digests, `pl_oracle --digest-v1` still prints the exact v1 bytes (it always
runs the Python path). Strategies with no reversals and no Set* exits keep the
same fills, so only the version line and the sha256 change.

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
windowed run that disagrees on trades fails the check, not a speed win.

## MultiCharts captures

Per-bar MultiCharts parity captures are licensed data and are not shipped.
Tests that need them skip cleanly on a fresh clone. See
[CAPTURES.md](CAPTURES.md) to attach your own.
