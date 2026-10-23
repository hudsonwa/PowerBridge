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

"""Native-vs-Python parity for the session-time words.

``SessionEndTime(...)``, ``SessionStartTime(...)`` and their siblings are
template constants in the Python runtime (``_rt_session``): they ignore their
arguments. The kernel must return the same values, so fills and traces match.
The bare ``SessionEndTime`` / ``SessionStartTime`` / ``Sess1StartTime`` forms
read run kwargs in Python instead and must still refuse.

Skips cleanly without the native extension.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
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

# The sample tape runs 0930..1149, so offsets from the session end put the
# entry and exit inside it.
EXIT_BEFORE_END = """
Vars: EndT(0);
EndT = SessionEndTime(1, 1);
If Time < EndT - 530 and MarketPosition = 0 Then Buy next bar at market;
If Time >= EndT - 500 Then Sell next bar at market;
"""

COMPARE_WITH_TIME = """
Inputs: Sess(1);
Vars: S(0), E(0);
S = SessionStartTime(Sess, 2);
E = SessionEndTime(0, Sess + 1);
If Time > S - 800 and Time < E Then Buy next bar at market;
If Time > E - 470 Then SellShort next bar at market;
"""

SIBLINGS = """
Vars: A(0), B(0), C(0);
A = Sess1EndTime - Sess1FirstBarTime + Sess2FirstBarTime;
B = SessionEndTimeMS(1) - SessionStartTimeMS(1) + Sess2StartTime;
C = Sess2EndTime - A;
If Time > B - 545 and C = 0 Then Buy next bar at market;
If Time > A - 400 Then Sell next bar at market;
"""

PROGRAMS = {
    "exit_before_end": EXIT_BEFORE_END,
    "compare_with_time": COMPARE_WITH_TIME,
    "siblings": SIBLINGS,
}


@pytest.fixture(scope="module")
def bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, bars):
    want = run_oracle(PROGRAMS[name], bars, NQ)
    assert want["fills"] > 0, "the program must trade for the digest to mean anything"
    assert fills_sha256(run_native(PROGRAMS[name], bars, NQ)) == want["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_trace_equals_python(name, bars):
    assert_trace_parity(PROGRAMS[name], bars)


@pytest.mark.parametrize("word", ["SessionEndTime", "SessionStartTime", "Sess1StartTime"])
def test_bare_kwarg_words_refuse(word, bars):
    src = f"Vars: X(0);\nX = {word};\nIf Time > X Then Buy next bar at market;\n"
    with pytest.raises(AccelRefused):
        run_native(src, bars, NQ)
