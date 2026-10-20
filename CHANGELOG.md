# Changelog

## Unreleased

- Strict mode is now the default: a run that calls an EasyLanguage builtin the
  runtime does not implement stops with a one-line error (builtin name, first
  source line, how to opt out) instead of using 0/''. Opt out with `--lenient`
  on `pl_run` / `pl_oracle` or `PL_STRICT=0`. Cosmetic builtins (drawing, sound,
  print) never stop a run. `PL_STRICT=0` / `false` / `no` / `off` now also turn
  off the per-bar re-raise in the bar loop instead of enabling it.

## 0.2.0 — 2026-10-20

- Distribution renamed `pl-transpiler` -> `powerbridge`; the Rust wheel is `powerbridge-accel`.
  The import name stays `pl_transpiler`.
- Optional Rust fast path (`accel/`, module `pl_transpiler._accel`): native bar
  loop and fill engine in one FFI call, Data2 support, batched and threaded
  input sweeps, prefix-skip and slice-before-marshal, typed trace bridge,
  in-process audit packets, `.pbar` binary tape sidecar.
- `pl_oracle` command: exact trade-list digest for parity gates.
- `ES` (@ES#C RTH 1-min) instrument config.
- Input overrides apply case-insensitively.
- `--from-date` / `--from-bar` windows with seeded warm-up on `pl_run` and `pl_oracle`.
- `persist=` / series-layout contract; arrays accepted by persist/trace.
- Validated against MultiCharts on 1-minute @ES (4 configurations, 4,278 fills identical).
- `pl_oracle` uses the Rust kernel by default when installed (`--python` forces Python);
  any Python-path run prints why. An installed accel module is found from inside a checkout.
- Unimplemented builtins used by a run are reported loudly (`PL_STRICT=1` still raises).
- Fixed: a strategy Input named like a reserved word (e.g. `ExitTime`) was read as the builtin.
- Fixed: `Input[n]` bar references crashed.
- Fixed: Inputs named `Len` / `Prec` were overwritten with 14 / 6 (Python and Rust).
- Fixed: `FormatDate`/`FormatTime` crashed on `ELDateToDateTime` values.
- Fixed: Rust kernel sized a quantity-less entry from the open position (fills doubled every bar).
- Fixed: tests and tools hard-coded `/usr/bin/python3` and `/usr/bin/perl` (Windows).

## 0.1.0 — 2026-07-15

- First public export: EasyLanguage ⇄ Python transpiler, `el_emit`, `pl_run`,
  `pl_transpile`, keyword catalog, fail-loud unsupported-keyword reporting.
