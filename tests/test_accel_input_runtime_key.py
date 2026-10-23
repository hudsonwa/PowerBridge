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

"""An input named like a runtime kwarg key falls back to Python (#153).

Python codegen reads an input as ``kwargs.get(name, default)``. When the
input's name is a key the runtime passes on every bar (``MaxShares`` is
``maxshares``), Python reads the runtime value, not the input. The native
kernel refuses such a read, bare or in bracket form, so the run falls back
to Python and the digest stays the Python one. The colliding keys are read
from a real Python run, so a key added to the runtime must be added to the
kernel's list too.

Skips cleanly without the native extension.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.engine import FillEngine, run_bar_loop  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    accel_available,
    explain_native,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
TRACE_TYPES_RS = os.path.join(REPO, "accel", "src", "strategy", "trace_types.rs")


def _runtime_kwargs():
    """Every kwargs key the Python runtime passes to a strategy, with a value."""
    seen = {}

    def strategy(*_series, **kwargs):
        seen.update(kwargs)
        return {"orders": [], "risk": {}}

    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    run_bar_loop(strategy, n_bars=2,
                 get_ohlcv=lambda i: (1.0, 1.1, 0.9, 1.05, 1.0),
                 dates=[1240102] * 2, times=[930, 931], config=NQ,
                 fill_engine=fe, emit_row=lambda *a: None, strict=True)
    return seen


RUNTIME_KWARGS = _runtime_kwargs()
RUNTIME_KEYS = sorted(RUNTIME_KWARGS)
# EL identifiers cannot start with "_", so only these can name an input.
INPUT_KEYS = [k for k in RUNTIME_KEYS if not k.startswith("_")]
# Reserved words: the parser reads these as the keyword even when declared as
# an input, so no input is read (and the bracket form is a syntax error).
PARSED_AS_KEYWORD = {
    "barinterval", "barnumber", "bartype", "bigpointvalue", "category",
    "currentbar", "currentsession", "datetime", "description",
    "expirationdate", "minmove", "pointvalue", "pricescale", "sess1starttime",
    "symbol", "symbolcurrencycode", "symbolname",
}
# OutPath is always "" in Python (its sandbox); the kernel reads "" too.
COLLIDING = [k for k in INPUT_KEYS if k not in PARSED_AS_KEYWORD | {"outpath"}]


def _kernel_keys():
    src = open(TRACE_TYPES_RS, encoding="utf-8").read()
    body = re.search(r"BAR_LOOP_KWARGS: \[&str; \d+\] = \[(.*?)\];", src, re.S)
    return sorted(re.findall(r'"([^"]+)"', body.group(1)))


def test_kernel_list_matches_runtime_keys():
    # OutPath is the kernel's BLANKED_INPUT: it reads "" like Python.
    assert _kernel_keys() == [k for k in RUNTIME_KEYS if k != "outpath"]


def _program(key, form):
    read = key + ("[1]" if form == "bracket" else "")
    if isinstance(RUNTIME_KWARGS.get(key), str):
        long_if, short_if = f'{read} = ""', f'{read} <> ""'
    else:
        long_if, short_if = f"{read} >= 0", f"{read} < 0"
    return """
Inputs: %s(2);
If MarketPosition = 0 and Close > Average(Close, 5) and %s Then Buy 1 contracts next bar at market;
If MarketPosition = 0 and Close < Average(Close, 5) and %s Then SellShort 1 contracts next bar at market;
If MarketPosition <> 0 and BarsSinceEntry >= 3 Then Begin Sell next bar at market; BuyToCover next bar at market; End;
""" % (key, long_if, short_if)


@pytest.fixture(scope="module")
def sample_bars():
    return load_bars_csv(BARS_CSV)


native = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)
FORMS = ("bare", "bracket")


@native
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("key", COLLIDING)
def test_colliding_input_is_refused(key, form, sample_bars):
    program = _program(key, form)
    messages = [r.message for r in explain_native(program)]
    assert any(f"input '{key}' collides with the runtime keyword {key}" in m
               for m in messages), messages
    with pytest.raises(AccelRefused):
        run_native(program, sample_bars, NQ)
    got = run_oracle(program, sample_bars, NQ, use_native=True)
    assert not got["native"]
    assert got["sha256"] == run_oracle(program, sample_bars, NQ)["sha256"]


@native
@pytest.mark.parametrize("key", sorted(PARSED_AS_KEYWORD))
def test_reserved_word_input_matches_python(key, sample_bars):
    program = _program(key, "bare")
    want = run_oracle(program, sample_bars, NQ)
    assert run_oracle(program, sample_bars, NQ, use_native=True)["sha256"] == want["sha256"]


# The reported case: the input sizes the order, so reading the runtime value
# (MaxShares is 0 when flat) instead of the input changes the trades.
@native
@pytest.mark.parametrize("read", ["MaxShares", "MaxShares[1]"])
def test_maxshares_input_as_quantity(read, sample_bars):
    program = """
Inputs: MaxShares(2);
If MarketPosition = 0 and Close > Average(Close, 5) Then Buy %s contracts next bar at market;
If MarketPosition = 1 and BarsSinceEntry >= 3 Then Sell next bar at market;
""" % read
    want = run_oracle(program, sample_bars, NQ)
    got = run_oracle(program, sample_bars, NQ, use_native=True)
    assert not got["native"]
    assert got["sha256"] == want["sha256"]


@native
@pytest.mark.parametrize("form", FORMS)
def test_outpath_input_reads_blank_natively(form, sample_bars):
    read = "OutPath" + ("[1]" if form == "bracket" else "")
    program = """
Inputs: OutPath("x");
If MarketPosition = 0 and %s = "" Then Buy 1 contracts next bar at market;
If MarketPosition = 0 and %s <> "" Then SellShort 1 contracts next bar at market;
If MarketPosition <> 0 and BarsSinceEntry >= 3 Then Begin Sell next bar at market; BuyToCover next bar at market; End;
""" % (read, read)
    assert explain_native(program) == []
    want = run_oracle(program, sample_bars, NQ)
    got = run_oracle(program, sample_bars, NQ, use_native=True)
    assert got["native"]
    assert got["sha256"] == want["sha256"]


# Inputs named like a keyword whose kwarg key differs still run natively.
@native
@pytest.mark.parametrize("name", ["ExitTime", "EntryPrice", "EntryDate", "Len"])
@pytest.mark.parametrize("form", FORMS)
def test_non_colliding_input_stays_native(name, form, sample_bars):
    program = _program(name, form)
    assert explain_native(program) == []
    got = run_oracle(program, sample_bars, NQ, use_native=True)
    assert got["native"]
    assert got["sha256"] == run_oracle(program, sample_bars, NQ)["sha256"]
