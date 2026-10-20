#!/usr/bin/env bash
#
# build_accel.sh — build the optional native accel extension (`pl_transpiler._accel`)
# from a clean checkout and install the cdylib next to the in-tree `pl_transpiler`
# package so `import pl_transpiler._accel` resolves without maturin/pip.
#
# The crate is `accel/` at the repo root; `cargo build --release` produces the
# cdylib (`lib_accel.dylib` on macOS, `lib_accel.so` on Linux). This script copies
# that artifact to `pl_transpiler/_accel<EXT_SUFFIX>` so the standard import
# mechanism finds it. It is the same artifact maturin would wheel; this script is
# the no-build-backend path from a clean checkout (no Rust dependency is added to
# the core `pl_transpiler` build).
#
#   ./tools/build_accel.sh
#
# Requirements: `cargo` on PATH and the platform Python (`python3`) that will do
# the import (its `EXT_SUFFIX` names the target file).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCEL_DIR="$REPO_ROOT/accel"

# The interpreter the built extension must import into. Defaults to `python3` on
# PATH; override with PYTHON=... to build against a specific interpreter (e.g. a
# venv). pyo3 links against this exact interpreter, so it must be the same one
# that will do `import pl_transpiler._accel`.
PYTHON="${PYTHON:-python3}"
PYTHON_BIN="$(command -v "$PYTHON")"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[build_accel] interpreter not found: $PYTHON" >&2
  exit 1
fi

PROFILE="${ACCEL_PROFILE:-release-lto}"

echo "[build_accel] cargo build --profile $PROFILE (in $ACCEL_DIR) against $PYTHON_BIN"
export PYO3_PYTHON="$PYTHON_BIN"
(cd "$ACCEL_DIR" && cargo build --profile "$PROFILE")

case "$(uname -s)" in
  Darwin) LIB_NAME="lib_accel.dylib" ;;
  Linux)  LIB_NAME="lib_accel.so" ;;
  *)
    echo "[build_accel] unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac

EXT_SUFFIX="$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
SRC="$ACCEL_DIR/target/$PROFILE/$LIB_NAME"
DST="$REPO_ROOT/pl_transpiler/_accel${EXT_SUFFIX}"

if [[ ! -f "$SRC" ]]; then
  echo "[build_accel] cdylib not found: $SRC" >&2
  exit 1
fi

cp "$SRC" "$DST"
echo "[build_accel] installed $DST"
