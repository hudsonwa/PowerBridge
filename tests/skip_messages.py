"""Shared skip messages, so every skip says how to enable the test."""

ACCEL_SKIP = "native kernel not built: run `maturin develop --release` in accel/"

AUDIT_TAPE_SKIP = (
    "audit tape not shipped; set PB_AUDIT_EL / PB_AUDIT_BARS / PB_AUDIT_D2 to run"
)
