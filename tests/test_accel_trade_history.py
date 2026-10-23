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

"""Native-vs-Python parity for the positions-ago trade history (#149).

EntryDate(n), EntryTime(n), EntryPrice(n), ExitDate(n), ExitTime(n),
ExitPrice(n) and MaxShares(n), in call and bracket form, read the n-th most
recently closed position (n = 0 is the current one, past the oldest is 0),
exactly like the Python runtime's ``pl_pos_ago``. The programs below trade
many times (scale-ins, reversals, a money-management stop, the session-close
exit) and feed every value into their orders, so a wrong value changes the
digest.

Skips cleanly without the native extension.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    accel_available,
    explain_native,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.test_accel_trace_parity import assert_trace_parity  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

ENTRIES = """
If MarketPosition = 0 and Close > Average(Close, 5) Then Buy ("L") 2 contracts next bar at market;
If MarketPosition = 1 and BarsSinceEntry = 1 Then Buy ("L2") 1 contracts next bar at market;
If MarketPosition = 0 and Close < Average(Close, 5) Then SellShort ("S") 1 contracts next bar at market;
If MarketPosition = 1 and BarsSinceEntry >= 4 Then Sell ("LX") 1 contracts next bar at market;
If MarketPosition = -1 and BarsSinceEntry >= 3 Then Buy ("R") 1 contracts next bar at market;
SetStopLoss(150);
SetExitOnClose;
"""

# Every field at every positions-ago index, both forms, at every bar (and
# so at the last bar), including n past the oldest closed position.
LOOP = """
Vars: K(0), Acc(0);
Acc = 0.0;
For K = 0 To 60 Begin
  If K <= TotalTrades + 1 Then
    Acc = Acc + EntryDate(K) + EntryTime(K) + EntryPrice(K) + ExitDate(K) + ExitTime(K)
      + ExitPrice(K) + MaxShares(K) + EntryTime[K] + ExitTime[K] + EntryDate[K] + ExitPrice[K];
End;
If MarketPosition = 0 and Mod(IntPortion(Acc), 3) = 0 Then Buy ("A") next bar at market;
""" + ENTRIES

# Mid-run reads with constant, negative and fractional indexes, traced.
FIELDS = """
Vars: ED1(0), ET1(0), EP1(0), XD1(0), XT1(0), XP1(0), MS1(0), ET0(0), MS0(0), EP2(0), XPN(0), ETF(0);
ED1 = EntryDate(1);
ET1 = EntryTime[1];
EP1 = EntryPrice(1);
XD1 = ExitDate(1);
XT1 = ExitTime[1];
XP1 = ExitPrice(1);
MS1 = MaxShares(1);
ET0 = EntryTime(0) + EntryDate + MaxShares + ExitDate + ExitTime;
MS0 = MaxShares[0];
EP2 = EntryPrice[2];
XPN = ExitPrice(-1);
ETF = EntryTime(1.7);
If MarketPosition = 0 and XP1 > EP1 and ET1 < 1200 Then SellShort ("W") next bar at market;
If MarketPosition = 0 and MS1 > 2 and XD1 = 0 Then Buy ("M") next bar at market;
""" + ENTRIES

PROGRAMS = {"loop": LOOP, "fields": FIELDS}


@pytest.fixture(scope="module")
def sample_bars():
    """The sample tape split over two sessions, so SetExitOnClose closes a trade."""
    loaded = load_bars_csv(BARS_CSV)
    bars = loaded[0] if isinstance(loaded, tuple) else loaded
    return [dict(bar, date=bar["date"] + (i >= 70)) for i, bar in enumerate(bars)]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_program_is_native(name):
    assert explain_native(PROGRAMS[name]) == []


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, sample_bars):
    want = run_oracle(PROGRAMS[name], sample_bars, NQ)
    assert want["closed_positions"] >= 5, "the program must close several positions"
    assert want["closed_positions"] < 60, "LOOP must reach past the oldest position"
    assert fills_sha256(run_native(PROGRAMS[name], sample_bars, NQ)) == want["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_history_drives_the_orders(name, sample_bars):
    # Without the history reads the program trades differently, so the
    # digest parity above covers the values.
    plain = PROGRAMS[name].replace("(1)", "(0)").replace("[1]", "[0]")
    plain = plain.replace("TotalTrades + 1", "-1")
    assert run_oracle(plain, sample_bars, NQ)["sha256"] != run_oracle(
        PROGRAMS[name], sample_bars, NQ)["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_trace_equals_python(name, sample_bars):
    assert_trace_parity(PROGRAMS[name], sample_bars)
