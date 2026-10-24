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

"""Native-vs-Python parity for the `total` and `from entry("X")` order modifiers (#125).

The Python engine never reads `total`, so it runs exactly like the same order
without it. A named exit closes its side as usual; with more than one entry
open on that side Python stops (strict) or warns (lenient, #24), and the
kernel refuses the run there so the caller falls back loudly. A label that is
not a string literal is refused at compile time.

Skips cleanly without the native extension.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime import _rt_core  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
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

# Partial `total` exits of multi-contract entries, market and stop.
TOTAL = """
If Close > Open Then Buy ("L") 3 contracts next bar at market;
If MarketPosition = 1 and Close < Open Then Sell ("LX") 1 contracts total next bar at market;
If Close < Open[1] and Close < Close[1] Then SellShort ("S") 2 contracts next bar at market;
If MarketPosition = -1 and Close > Open Then BuyToCover ("SX") 1 contracts total next bar at Close + 1 stop;
"""

# Named exits with one entry open: a partial exit, a stop exit, an exit from
# an entry that is not open, and a this-bar-on-close exit.
NAMED = """
If MarketPosition = 0 and Close > Open Then Buy ("L") 2 contracts next bar at market;
If MarketPosition = 1 and Close < Open Then Sell ("LX1") 1 contracts from entry ("L") next bar at market;
If MarketPosition = 1 and BarsSinceEntry > 3 Then Sell ("LX2") from entry ("L") next bar at Low - 1 stop;
If MarketPosition = 0 and Close < Open[1] Then SellShort ("S") 1 contracts next bar at market;
If MarketPosition = -1 Then BuyToCover ("SX") from entry ("S") next bar at High + 2 stop;
If MarketPosition = -1 Then BuyToCover ("SZ") 1 contracts total from entry ("NOPE") this bar on close;
"""

PROGRAMS = {"total": TOTAL, "named": NAMED}

# Two entries open when the named exit fills (#24).
TWO_LONG = (
    'If BarNumber = 5 then Buy ("A") 1 contract next bar at market;\n'
    'If BarNumber = 6 then Buy ("B") 1 contract next bar at market;\n'
    'If BarNumber = 10 then Sell ("XA") from entry ("A") next bar at market;\n'
)

NON_LITERAL = """
Vars: N("A");
If Close > Open Then Buy ("A") next bar at market;
If Close < Open Then Sell from entry (N) next bar at market;
"""


@pytest.fixture(scope="module")
def sample_bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_program_is_native(name):
    assert explain_native(PROGRAMS[name]) == []


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_digest_equals_python(name, sample_bars):
    want = run_oracle(PROGRAMS[name], sample_bars, NQ, max_bars_back=0)
    assert want["fills"] > 0, "the program must trade for the digest to mean anything"
    assert fills_sha256(run_native(PROGRAMS[name], sample_bars, NQ)) == want["sha256"]


def test_total_is_a_no_op(sample_bars):
    plain = TOTAL.replace(" total", "")
    assert run_oracle(plain, sample_bars, NQ, max_bars_back=0)["sha256"] == run_oracle(TOTAL, sample_bars, NQ, max_bars_back=0)["sha256"]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_trace_equals_python(name, sample_bars):
    assert_trace_parity(PROGRAMS[name], sample_bars)


def test_named_exit_with_two_entries_is_refused(sample_bars):
    with pytest.raises(AccelRefused, match=r"'from entry' \(Sell from entry\(\"A\"\)\)"):
        run_native(TWO_LONG, sample_bars[:40], NQ)


def test_named_exit_with_two_entries_falls_back_lenient(monkeypatch, sample_bars):
    monkeypatch.setattr(_rt_core, "_PL_STRICT", False)
    _rt_core.pl_unimplemented_reset()
    try:
        want = run_oracle(TWO_LONG, sample_bars[:40], NQ, max_bars_back=0)
        got = run_oracle(TWO_LONG, sample_bars[:40], NQ, use_native=True, max_bars_back=0)
    finally:
        _rt_core.pl_unimplemented_reset()
    assert got["sha256"] == want["sha256"]
    assert "from entry" in (got["native_fallback_reason"] or "")


def test_non_literal_label_names_the_modifier():
    (refusal,) = explain_native(NON_LITERAL)
    assert (refusal.kind, refusal.name) == ("order", "from entry")
    assert "'from entry' with a non-literal label" in refusal.message
