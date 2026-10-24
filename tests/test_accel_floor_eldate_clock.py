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

"""Native-vs-Python parity for Floor, ELDateToDateTime and the clock words.

``Floor`` is ``math.floor`` (an int, rounding negatives down).
``ELDateToDateTime(d)`` is the runtime's ``pl_datetojulian`` and
``ELDateToDateTime(d, t)`` is ``d * 10000 + t``. ``CurrentTime_s``,
``CurrentDate_s`` and ``ComputerDateTime`` read the computer clock through
``_rt_datetime._clock_now``; the kernel calls the same function, so patching
it pins both paths to one time.

Skips cleanly without the native extension.
"""
import datetime
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime import _rt_datetime  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    accel_available,
    explain_native,
    run_native,
    run_native_trace,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.test_accel_trace_parity import assert_trace_parity  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

# Negative operands, exact integers and fractional quarters.
FLOOR = """
Vars: A(0), B(0), C(0);
A = Floor(Close / 7);
B = Floor(-Close / 3) + Floor(-2.5) + Floor(2.5) + Floor(-0.0);
C = Floor((Close - Open) * 2) - Floor(-(High - Low) / 2);
If C > 0 and MarketPosition = 0 Then Buy next bar at market;
If C < 0 and A + B > -10 Then Sell next bar at market;
If Floor(Close) < Floor(Close[2]) Then SellShort next bar at market;
If Floor(Close / 2) > Floor(Close[1] / 2) Then BuyToCover next bar at market;
"""

# Bar dates, an out-of-range month and day, and the two-argument form.
ELDATE = """
Vars: J(0), K(0), Q(0);
J = ELDateToDateTime(Date);
K = ELDateToDateTime(1241399) - ELDateToDateTime(1240000) + ELDateToDateTime(-1);
Q = ELDateToDateTime(124, 305) + ELDateToDateTime(Date / 10000, Time);
If Mod(J, 3) = 0 and Close > Open Then Buy next bar at market;
If Mod(J, 3) = 1 and K > 0 and Q > 0 Then Sell next bar at market;
If J > ELDateToDateTime(Date[1]) Then SellShort next bar at market;
If Mod(J, 3) = 2 Then BuyToCover next bar at market;
"""

# The clock decides trades, so a mismatch shows in the digest.
CLOCK = """
Vars: T(0), D(0), X(0);
T = CurrentTime_s;
D = CurrentDate_s;
X = ComputerDateTime;
If Mod(T + BarNumber, 2) = 1 and D > 0 Then Buy next bar at market;
If Mod(CurrentTime_s() + BarNumber, 2) = 0 and X > 0 Then Sell next bar at market;
"""

PROGRAMS = {"floor": FLOOR, "eldate": ELDATE, "clock": CLOCK}

FIXED_NOW = datetime.datetime(2024, 3, 5, 9, 7, 3, 250000)


@pytest.fixture(scope="module")
def sample_bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(_rt_datetime, "_clock_now", lambda: FIXED_NOW)


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_program_is_native(name):
    assert explain_native(PROGRAMS[name]) == []


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, sample_bars, fixed_clock):
    want = run_oracle(PROGRAMS[name], sample_bars, NQ, max_bars_back=0)
    assert want["fills"] > 0, "the program must trade for the digest to mean anything"
    assert fills_sha256(run_native(PROGRAMS[name], sample_bars, NQ)) == want["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_trace_equals_python(name, sample_bars, fixed_clock):
    assert_trace_parity(PROGRAMS[name], sample_bars)


def test_clock_follows_the_patched_time(sample_bars, monkeypatch):
    """An odd and an even second give different digests on both paths."""
    digests = []
    for second in (3, 4):
        now = FIXED_NOW.replace(second=second)
        monkeypatch.setattr(_rt_datetime, "_clock_now", lambda now=now: now)
        native = fills_sha256(run_native(CLOCK, sample_bars, NQ))
        assert native == run_oracle(CLOCK, sample_bars, NQ, max_bars_back=0)["sha256"]
        digests.append(native)
    assert digests[0] != digests[1]


def test_real_clock_values_have_runtime_types(sample_bars):
    columns, rows, _ = run_native_trace(CLOCK, sample_bars, NQ)
    index = {name.lower(): i for i, name in enumerate(columns)}
    for row in rows:
        t, d, x = (row[index[c]] for c in ("t", "d", "x"))
        assert 0 <= float(t) <= 235959 and float(t) == int(float(t))
        assert float(d) > 1240000
        assert float(x) > 45000
