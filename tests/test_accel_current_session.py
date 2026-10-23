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

"""Native-vs-Python parity for CurrentSession and the session-count words.

On the Python path ``bar_loop`` passes ``CurrentSession`` as the ISO weekday
of the bar's date (``engine_dates._el_date_to_day_of_week``); the kernel
computes the same value. ``SessionCount``, ``SessionCountMS``,
``SessionEndDay`` and ``SessionEndDayMS`` are template constants in
``_rt_session``. The sample tape is re-dated across a full week (Monday to
Sunday) so every weekday value is seen.

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
# 2024-01-01 (Monday) .. 2024-01-07 (Sunday), 20 bars a day.
WEEK = [1240101 + d for d in range(7)]

# Trade only in one session (Wednesday), flatten near SessionEndTime.
ONE_SESSION = """
Vars: S(0);
S = CurrentSession(0);
If S = 3 and Time < SessionEndTime(1, 1) - 530 and MarketPosition = 0 Then
    Buy next bar at market;
If S <> 3 Then Sell next bar at market;
"""

# Bare form, an expression argument, and the weekday as a quantity.
BARE_AND_ARGS = """
Inputs: Sess(1);
Vars: S(0), Q(0);
S = CurrentSession;
Q = CurrentSession(Sess + 1) + 0 * Sess;
If S = Q and S <= 5 and MarketPosition = 0 Then Buy S contracts next bar at market;
If MarketPosition = 1 and S >= 6 Then Sell next bar at market;
"""

COUNTS = """
Vars: N(0), E(0);
N = SessionCount(1) + SessionCountMS;
E = SessionEndDay(1, 1) + SessionEndDayMS(1) + SessionEndDay + SessionCount;
If N = 10 and E = 8 and CurrentSession >= E - 6 Then SellShort next bar at market;
If CurrentSession = 1 Then BuyToCover next bar at market;
"""

PROGRAMS = {
    "one_session": ONE_SESSION,
    "bare_and_args": BARE_AND_ARGS,
    "counts": COUNTS,
}


def _load(path):
    loaded = load_bars_csv(path)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.fixture(scope="module")
def sample_bars():
    return _load(BARS_CSV)


@pytest.fixture(scope="module")
def week_bars(sample_bars):
    bars = [dict(bar) for bar in sample_bars]
    for i, bar in enumerate(bars):
        bar["date"] = WEEK[min(i // 20, len(WEEK) - 1)]
    return bars


@pytest.mark.parametrize("tape", ["sample", "week"])
@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, tape, sample_bars, week_bars):
    bars = week_bars if tape == "week" else sample_bars
    want = run_oracle(PROGRAMS[name], bars, NQ)
    if tape == "week":
        assert want["fills"] > 0, "the program must trade for the digest to mean anything"
    assert fills_sha256(run_native(PROGRAMS[name], bars, NQ)) == want["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_trace_equals_python(name, week_bars):
    assert_trace_parity(PROGRAMS[name], week_bars)

