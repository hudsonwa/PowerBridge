#!/usr/bin/env python3
# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""SPIKE tests for the typed audit-event-v1 packet.

Skipped when ``pl_transpiler._accel`` is not built.
"""
import os
import sys

try:
    import pytest
except ImportError:  # 3.14 CI image has no pytest
    pytest = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    accel_available,
    run_native,
    run_native_audit_packet,
)
from pl_transpiler.runtime.audit_packet import (  # noqa: E402
    TRANSITION_SIZE,
    decode,
    refuse_missing_columns,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")
SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

SLOTS = """
Vars:
    PriorSessionClose(0),
    PriorSessionUnadjustedClose(0),
    PriorSessionTR(0),
    PriorSessionWeightedMean(0),
    C3EntryLevel(0),
    C3ScaledLevel(0),
    C4CappedRange(0),
    C4TrailDistance(0),
    HistoryReady(False),
    C1Gate(False),
    EntryGate(False),
    LadderBarsElapsed(0),
    BestCloseSinceSignal(0),
    TrailStop(0),
    EmergencyExit(False),
    EodExit(False);
"""

TINY = (
    SLOTS
    + """
If BarNumber = 2 Then Buy ("LE") 1 Contract Next Bar At Market;
If BarNumber = 6 Then Sell ("LX") Next Bar At Market;
"""
)

# Schema item 6 B: these two are assigned in EL but never read; the packet
# must not require them.
SLOTS_WITHOUT_DROPPED = """
Vars:
    PriorSessionClose(0),
    PriorSessionUnadjustedClose(0),
    PriorSessionTR(0),
    C3ScaledLevel(0),
    C4CappedRange(0),
    C4TrailDistance(0),
    HistoryReady(False),
    C1Gate(False),
    EntryGate(False),
    LadderBarsElapsed(0),
    BestCloseSinceSignal(0),
    TrailStop(0),
    EmergencyExit(False),
    EodExit(False);
"""

TINY_WITHOUT_DROPPED = (
    SLOTS_WITHOUT_DROPPED
    + """
If BarNumber = 2 Then Buy ("LE") 1 Contract Next Bar At Market;
If BarNumber = 6 Then Sell ("LX") Next Bar At Market;
"""
)


def _require_accel():
    if not accel_available():
        if pytest is not None:
            pytest.skip(ACCEL_SKIP)
        raise SystemExit("skip: " + ACCEL_SKIP)


def test_tiny_el_packet_matches_run_native_fills():
    _require_accel()
    bars = load_bars_csv(SAMPLES)
    fills = run_native(TINY, bars, NQ)
    packet = run_native_audit_packet(TINY, bars, NQ)
    decoded = decode(packet)
    header = decoded["header"]
    assert header["fill_count"] == len(fills)
    assert header["remaining_open"] == 0
    assert header["trade_count"] == 1
    assert header["hash_fills"] == fills_sha256(fills)
    assert decoded["trades"][0]["contracts"] == 1
    assert decoded["trades"][0]["bars_held"] > 0
    trans = decoded["transitions"][0]
    assert "priorsessionweightedmean" not in trans
    assert "c3entrylevel" not in trans
    assert TRANSITION_SIZE == 104
    expected = 256 + header["transition_count"] * 104 + header["trade_count"] * 88
    assert len(packet) == expected


def test_packet_does_not_require_dropped_slots():
    _require_accel()
    bars = load_bars_csv(SAMPLES)
    packet = run_native_audit_packet(TINY_WITHOUT_DROPPED, bars, NQ)
    decoded = decode(packet)
    assert decoded["header"]["trade_count"] == 1


def test_refuse_missing_columns_raises_on_dropped_and_dates():
    try:
        refuse_missing_columns(["current_contracts", "priorsessionweightedmean"])
    except ValueError as exc:
        assert "priorsessionweightedmean" in str(exc)
    else:
        raise AssertionError("expected ValueError for dropped column")
    try:
        refuse_missing_columns(["_date", "_time", "_close"])
    except ValueError as exc:
        assert "_date" in str(exc)
    else:
        raise AssertionError("expected ValueError for tape-recovered columns")
    refuse_missing_columns(["current_contracts", "priorsessiontr", "c3scaledlevel"])


if __name__ == "__main__":
    test_tiny_el_packet_matches_run_native_fills()
    print("test_tiny_el_packet_matches_run_native_fills OK")
    test_packet_does_not_require_dropped_slots()
    print("test_packet_does_not_require_dropped_slots OK")
    test_refuse_missing_columns_raises_on_dropped_and_dates()
    print("test_refuse_missing_columns_raises_on_dropped_and_dates OK")
