#!/usr/bin/env python3
# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
test_fill_log.py — the fill log records every change of the signed position.

A reversal logs its close leg and then its open leg, Set* risk exits are
logged, and round_turns counts exit records. ``--digest-v1`` still gives the
pre-v2 bytes. When the native kernel is installed its fills must match
(the parity test skips, with a reason, when it is not).

Stdlib only; runs on the shipped 140-bar sample tape.
"""
import contextlib
import io
import os
import sys
import tempfile

try:
    import pytest
except ImportError:  # standalone run without pytest
    pytest = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import (  # noqa: E402
    fill_trade_types,
    run_oracle,
)
from pl_transpiler.tools.pl_oracle import (
    main as oracle_main,
)
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

REVERSAL_EL = (
    "If CurrentBar = 5 Then Buy Next Bar at Market;\n"
    "If CurrentBar = 10 Then SellShort Next Bar at Market;\n"
    "If CurrentBar = 15 Then BuyToCover Next Bar at Market;\n"
)
LONG2_SHORT1_EL = (
    "If CurrentBar = 5 Then Buy 2 Contracts Next Bar at Market;\n"
    "If CurrentBar = 10 Then SellShort 1 Contracts Next Bar at Market;\n"
)
STOP_LOSS_EL = (
    "If MarketPosition = 0 Then Buy 2 Contracts Next Bar at Market;\n"
    "SetStopLoss(500);\n"
)
PROFIT_TARGET_EL = (
    "If MarketPosition = 0 Then Buy Next Bar at Market;\n"
    "SetProfitTarget(200);\n"
)
# The reversal repro as a v1 digest: the reversal's open leg and the cover are
# missing. Pinned so --digest-v1 keeps producing the pre-v2 bytes.
REVERSAL_DIGEST_V1 = (
    "powerbridge-trade-digest v1\n"
    "fills: 2\n"
    "round_turns: 1\n"
    "sha256: 59a9f261c75a4b3665046c6d352bb8abf3d2b36f2230afe22921f521ea8fe641\n"
)

_BARS = None


def _bars():
    global _BARS
    if _BARS is None:
        _BARS = load_bars_csv(SAMPLE)
    return _BARS


def _run(el):
    """Python-path digest."""
    return run_oracle(el, _bars(), NQ, use_native=False)


def _sides(d):
    return [(r["type"], r["signal"]) for r in d["records"]]


def test_reversal_logs_exit_and_entry_legs():
    d = _run(REVERSAL_EL)
    # Three orders: Buy, SellShort (reversal = exit leg + entry leg), BuyToCover.
    assert d["fills"] == 4
    assert fill_trade_types(d["records"]) == [
        "EntryLong", "ExitLong", "EntryShort", "ExitShort"]
    assert [r["type"] for r in d["records"]] == [
        "EntryLong", "ExitLong", "ExitLong", "EntryLong"]
    rev = d["records"][1:3]
    assert rev[0]["time"] == rev[1]["time"] and rev[0]["price"] == rev[1]["price"]
    assert d["round_turns"] == 2
    assert d["text"].startswith("powerbridge-trade-digest v2\n")
    print("  [ok] Buy / SellShort / BuyToCover -> 4 records, 2 round turns")


def test_long_two_to_short_one():
    d = _run(LONG2_SHORT1_EL)
    assert fill_trade_types(d["records"]) == [
        "EntryLong", "EntryLong", "ExitLong", "ExitLong", "EntryShort"]
    assert d["round_turns"] == 2
    print("  [ok] long 2 -> short 1 -> 2 exits + 1 entry")


def test_set_stop_loss_exits_logged():
    d = _run(STOP_LOSS_EL)
    exits = [r for r, t in zip(d["records"], fill_trade_types(d["records"]))
             if t == "ExitLong"]
    assert exits and all(r["signal"] == "stop_loss" for r in exits)
    # Two contracts per stop-out, each one a record.
    assert len(exits) % 2 == 0
    assert d["round_turns"] == len(exits)
    print(f"  [ok] SetStopLoss: {len(exits)} exit records")


def test_set_profit_target_exits_logged():
    d = _run(PROFIT_TARGET_EL)
    labels = fill_trade_types(d["records"])
    exits = [r for r, t in zip(d["records"], labels) if t == "ExitLong"]
    assert exits and all(r["signal"] == "profit_target" for r in exits)
    assert d["round_turns"] == len(exits)
    assert d["fills"] - len(exits) in (len(exits), len(exits) + 1)  # last may stay open
    print(f"  [ok] SetProfitTarget: {len(exits)} exit records")


def test_native_fills_match_python():
    """With the native kernel installed, its fills and digest match Python."""
    if not accel_available():
        # pytest.skip only under pytest; as a plain script (the public tier)
        # it would raise and count as a failure.
        if pytest is not None and "PYTEST_CURRENT_TEST" in os.environ:
            pytest.skip(ACCEL_SKIP)
        print("  skip: " + ACCEL_SKIP)
        return
    for el in (REVERSAL_EL, LONG2_SHORT1_EL, STOP_LOSS_EL, PROFIT_TARGET_EL):
        py = _run(el)
        nat = run_oracle(el, _bars(), NQ, use_native=True)
        assert nat.get("native"), nat.get("native_fallback_reason")
        assert nat["text"] == py["text"], (nat["text"], py["text"])
        assert fill_trade_types(nat["records"]) == fill_trade_types(py["records"])


def test_run_el_trades_match_oracle():
    d = run_oracle(REVERSAL_EL, _bars(), NQ, use_native=False)
    traced = run_el(REVERSAL_EL, _bars(), NQ, retain_rows=False)
    assert traced["trades"] == d["records"]
    print("  [ok] run_el trades == oracle records")


def test_digest_v1_reproduces_old_bytes():
    d = run_oracle(REVERSAL_EL, _bars(), NQ, digest_version=1)
    assert d["text"] == REVERSAL_DIGEST_V1, d["text"]
    with tempfile.TemporaryDirectory() as td:
        el_path = os.path.join(td, "rev.el")
        with open(el_path, "w", encoding="utf-8") as f:
            f.write(REVERSAL_EL)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = oracle_main([el_path, SAMPLE, "--digest-v1"])
    assert rc == 0
    assert out.getvalue() == REVERSAL_DIGEST_V1
    print("  [ok] --digest-v1 reproduces the pre-v2 digest")


TESTS = [
    test_reversal_logs_exit_and_entry_legs,
    test_long_two_to_short_one,
    test_set_stop_loss_exits_logged,
    test_set_profit_target_exits_logged,
    test_native_fills_match_python,
    test_run_el_trades_match_oracle,
    test_digest_v1_reproduces_old_bytes,
]


def main():
    for t in TESTS:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL FILL LOG TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
