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

"""Native-vs-Python parity for Round, MinMove/PriceScale and LastBarOnChart_s.

``Round(x, n)`` is the runtime's ``el_round`` (half away from zero on
``x * 10 ** n``, so a negative ``n`` rounds to tens, hundreds, ...).
``MinMove`` and ``PriceScale`` are the instrument config values the bar loop
passes in, and ``LastBarOnChart_s`` is the runtime constant ``True``.

Skips cleanly without the native extension.
"""
import dataclasses
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from tests.test_accel_trace_parity import assert_trace_parity  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    accel_available,
    explain_native,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

NQ = get_config("NQ")
# A config whose MinMove/PriceScale differ from NQ's, so a hard-coded value
# in the kernel would show up as a different digest.
CENTS = dataclasses.replace(NQ, minmove=1, pricescale=100)
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

# Negative values, exact .5 halves (bar ranges are quarter multiples) and
# negative precision.
ROUND = """
Vars: A(0), B(0), C(0), D(0), H(0);
A = Round(Close / 7, 2);
B = Round(-Close / 3, 0);
C = Round(Close, -2) + Round(-2.5, 0) + Round(2.5, 0) + Round(-1250, -2);
D = Round((Close - Open) * 2, 0);
H = Round(-(High - Low) / 2, 1) + Round(Close, -1) - Round(Close[1], -1);
If D > 0 and H < 50 and MarketPosition = 0 Then Buy next bar at market;
If D < 0 and A > B Then Sell next bar at market;
If C > Round(Close, -3) Then SellShort next bar at market;
If D >= 1 Then BuyToCover next bar at market;
"""

MINMOVE = """
Vars: T(0), P(0);
T = MinMove / PriceScale;
P = MinMove * PriceScale;
If MarketPosition = 0 and P > 0 Then Buy next bar at Close + 4 * T stop;
If MarketPosition = 1 Then Sell next bar at Close - 2 Points stop;
"""

LASTBAR = """
Vars: L(false);
L = LastBarOnChart_s;
If L and Close > Open Then Buy next bar at market;
If L and Close < Open Then Sell next bar at market;
If not LastBarOnChart_s Then SellShort next bar at market;
"""

PROGRAMS = {"round": ROUND, "minmove": MINMOVE, "lastbar": LASTBAR}


@pytest.fixture(scope="module")
def sample_bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_program_is_native(name):
    assert explain_native(PROGRAMS[name]) == []


@pytest.mark.parametrize("config", [NQ, CENTS], ids=["NQ", "cents"])
@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, config, sample_bars):
    want = run_oracle(PROGRAMS[name], sample_bars, config)
    assert want["fills"] > 0, "the program must trade for the digest to mean anything"
    assert fills_sha256(run_native(PROGRAMS[name], sample_bars, config)) == want["sha256"]


def test_minmove_changes_the_digest(sample_bars):
    nq = run_oracle(MINMOVE, sample_bars, NQ)["sha256"]
    assert run_oracle(MINMOVE, sample_bars, CENTS)["sha256"] != nq


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_trace_equals_python(name, sample_bars):
    assert_trace_parity(PROGRAMS[name], sample_bars)
