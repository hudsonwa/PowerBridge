#!/bin/sh
#
# dev_setup.sh: set up a full PowerBridge development environment in the
# active virtual environment.
#
#   python3 -m venv .venv && . .venv/bin/activate
#   ./tools/dev_setup.sh
#
# It upgrades pip, installs the package in editable mode with the dev tools
# (`pip install -e '.[dev]'`), and, when `cargo` is on PATH, builds the native
# kernel with tools/build_accel.sh. Without a Rust toolchain it says so and
# carries on: everything except the `needs_native` tests still runs.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "[dev_setup] no virtual environment is active." >&2
  echo "[dev_setup] create and activate one first, for example:" >&2
  echo "[dev_setup]   python3 -m venv .venv && . .venv/bin/activate" >&2
  exit 1
fi

PYTHON="python"
echo "[dev_setup] using $(command -v "$PYTHON") ($VIRTUAL_ENV)"

echo "[dev_setup] upgrading pip"
"$PYTHON" -m pip install --quiet --upgrade pip

echo "[dev_setup] pip install -e '.[dev]'"
(cd "$REPO_ROOT" && "$PYTHON" -m pip install --quiet -e '.[dev]')

if command -v cargo >/dev/null 2>&1; then
  echo "[dev_setup] cargo found; building the native kernel"
  PYTHON="$PYTHON" "$REPO_ROOT/tools/build_accel.sh"
else
  echo "[dev_setup] skipped the native kernel: cargo is not on PATH."
  echo "[dev_setup] the needs_native tests will skip. To build it, install Rust"
  echo "[dev_setup] (https://rustup.rs) and rerun this script, or run"
  echo "[dev_setup] tools/build_accel.sh (see docs/ACCEL.md)."
fi

"$PYTHON" -c 'from pl_transpiler.runtime.accel import native_status; print("[dev_setup] done. " + native_status()[1])'
