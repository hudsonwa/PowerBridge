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

# AGENTS.md — PowerBridge guide for coding agents

PowerBridge transpiles MultiCharts PowerLanguage / EasyLanguage (EL) strategies to
Python, emits EL back from its AST or from a bounded Python dialect, and backtests EL
over OHLCV bars with exact, digest-checked trade lists. The human guide is
[README.md](README.md); the full API, CLI and invariant reference is
[docs/dev/agent-guide-details.md](docs/dev/agent-guide-details.md).

## Layout

- `pl_transpiler/` — the package (pure standard library).
  - `transpile` (EL → Python), `reverse` (AST/Python → EL), `py_front` (Python → AST),
    `catalog` (EL keyword catalog).
  - `tools/` — CLIs: `pl_transpile`, `el_emit`, `pl_run`, `pl_oracle`, plus the
    `mc_ground_check` compile-safety check.
  - `runtime/` — per-bar engine, instrument config, `.pbar` bar tape, native-kernel caller.
- `accel/` — optional Rust native kernel ([docs/ACCEL.md](docs/ACCEL.md)).
- `tests/` — verification gates (not packaged into the wheel).
- `ground_truth/`, `emitted/`, `examples/` — EL sources and synthetic sample data.

The shared intermediate representation is the parser's dict AST.

## Build and test

Run from the repository root:

```bash
pip install .
python3 -m pip install pytest pytest-timeout ruff
python3 tests/run_public_tier.py   # portable gate; green without MultiCharts captures
pytest -q --timeout=60
ruff check .
```

Native kernel (only if you change `accel/`): `cd accel && cargo fmt --check &&
cargo clippy --all-targets -- -D warnings && cargo test --release`.

Capture-gated tests skip (never fail) when the MultiCharts captures are absent; see
[docs/CAPTURES.md](docs/CAPTURES.md).

## Conventions

- **Fail loud.** Anything that cannot be represented faithfully raises; never emit or
  run a plausible-but-wrong result.
- **Byte-identical refactors.** Refactors keep emitted code, stdout formats and trade
  digests unchanged.
- **Float32 semantics** at a fixed `1e-6` tolerance; never loosen it.
- **Strict by default.** `--partial` output is for incremental porting only and is not
  faithful to the source.
- Keep changes small and focused; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Safety gates before a change lands

1. `python3 tests/run_public_tier.py` and `pytest -q --timeout=60` pass.
2. Engine, codegen, parser or runtime changes: `pl_oracle` digests match with and
   without `--python` on `examples/` and `ground_truth/GT2*`.
3. `accel/` changes: the cargo checks above pass.
4. A nonzero exit from `el_emit` means the emitted EL failed `mc_ground_check`; do not
   treat it as compile-safe.
5. Only synthetic market data in the repository; no licensed data or private paths.
   Security reports: [SECURITY.md](SECURITY.md).

## Licensing

Apache-2.0 ([LICENSE](LICENSE)); third-party carve-outs and trademarks are in
[NOTICE](NOTICE) and the details guide.
