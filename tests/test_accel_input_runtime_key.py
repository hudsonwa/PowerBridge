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
# OutPath is always "" in Python (its sandbox); see the OutPath test below.
COLLIDING = [k for k in INPUT_KEYS if k not in PARSED_AS_KEYWORD | {"outpath"}]


def _oracle_extra_keys():
    """Keys pl_oracle's per_bar_extra hook adds (it only has one with Data2)."""
    # run_oracle drives the loop through pl_transpiler.run's shared helper.
    import pl_transpiler.run as run_mod
    seen = set()
    real = run_mod.run_bar_loop

    def spy(*args, per_bar_extra=None, **kwargs):
        if per_bar_extra is not None:
            seen.update(per_bar_extra(0))
        return real(*args, per_bar_extra=per_bar_extra, **kwargs)

    bars = load_bars_csv(BARS_CSV)
    run_mod.run_bar_loop = spy
    try:
        run_oracle("Value1 = Close;", bars, NQ, data2_closes=[b["close"] for b in bars])
    finally:
        run_mod.run_bar_loop = real
    return seen


def _gt_harness_extra_keys():
    """Keys tests/run_gt.py's per-bar hook sets (`ex['key'] = ...`)."""
    src = open(os.path.join(REPO, "tests", "run_gt.py"), encoding="utf-8").read()
    hook = src[src.index("def _per_bar_extra"):src.index("return ex", src.index("def _per_bar_extra"))]
    keys = set(re.findall(r"ex\['(\w+)'\]", hook))
    keys |= set(re.findall(r"'\w+': '(\w+)'", hook))
    return keys


# Len/Prec are the GT1 capture's own per-bar input values: there the hook
# feeds the input, it does not shadow it.
GT_INPUT_FEEDS = {"len", "prec"}
EXTRA_KEYS = sorted(
    (_oracle_extra_keys() | _gt_harness_extra_keys()) - set(RUNTIME_KEYS) - GT_INPUT_FEEDS)


def _kernel_keys(const="BAR_LOOP_KWARGS"):
    src = open(TRACE_TYPES_RS, encoding="utf-8").read()
    body = re.search(const + r": \[&str; \d+\] = \[(.*?)\];", src, re.S)
    return sorted(re.findall(r'"([^"]+)"', body.group(1)))


def test_kernel_list_matches_runtime_keys():
    # OutPath is the kernel's BLANKED_INPUT, handled on its own.
    assert _kernel_keys() == [k for k in RUNTIME_KEYS if k != "outpath"]


def test_kernel_extra_list_matches_runner_hooks():
    assert "data2_close" in EXTRA_KEYS
    assert _kernel_keys("PER_BAR_EXTRA_KEYS") == EXTRA_KEYS


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


# Ticks/UpTicks/DownTicks/OpenInt parse as the keyword even when declared as
# an input; the kernel refuses that keyword, so only the bare form is checked.
EXTRA_PARSED_AS_KEYWORD = {"downticks", "openint", "ticks", "upticks"}


@native
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("key", [k for k in EXTRA_KEYS if k not in EXTRA_PARSED_AS_KEYWORD])
def test_runner_extra_key_input_is_refused(key, form):
    messages = [r.message for r in explain_native(_program(key, form))]
    assert any(f"input '{key}' collides with the runtime keyword {key}" in m
               for m in messages), messages


@native
@pytest.mark.parametrize("key", sorted(EXTRA_PARSED_AS_KEYWORD))
def test_runner_extra_keyword_input_is_refused(key):
    assert explain_native(_program(key, "bare"))


def _data2_tape(bars):
    """A synthetic Data2 tape: the sample bars, shifted up by 5 points."""
    return [dict(b, open=b["open"] + 5, high=b["high"] + 5,
                 low=b["low"] + 5, close=b["close"] + 5) for b in bars]


# With Data2, pl_oracle sets `data2_close` per bar, so Python reads Data2's
# close for this input; the native run used the input's default.
@native
def test_data2_close_input_matches_python_with_data2(sample_bars):
    program = """
Inputs: Data2_Close(5);
If MarketPosition = 0 and Close of data2 > Data2_Close Then Buy 1 contracts next bar at market;
If MarketPosition = 1 and BarsSinceEntry >= 3 Then Sell next bar at market;
"""
    d2 = _data2_tape(sample_bars)
    want = run_oracle(program, sample_bars, NQ, data2_bars=d2)
    got = run_oracle(program, sample_bars, NQ, data2_bars=d2, use_native=True)
    assert not got["native"]
    assert got["sha256"] == want["sha256"]


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


# OutPath is always "" in Python (its sandbox), never the input's value (#155).
# The kernel refuses every read outside a file path, bare or in bracket form,
# so the native run falls back and fails or runs exactly as Python does.
OUTPATH_NUMERIC = {
    "compare": "If MarketPosition = 0 and {o} > 2 Then Buy 1 contracts next bar at market;",
    "string_compare": 'If MarketPosition = 0 and {o} = "" Then Buy 1 contracts next bar at market;',
    "arithmetic": "Value1 = {o} + 1;",
    "condition": "If MarketPosition = 0 and {o} Then Buy 1 contracts next bar at market;",
    "quantity": "If MarketPosition = 0 Then Buy {o} contracts next bar at market;",
}


def _oracle_outcome(program, bars, **kw):
    try:
        return run_oracle(program, bars, NQ, **kw)["sha256"]
    except Exception as exc:  # noqa: BLE001 - parity of the exception itself
        return (type(exc), str(exc))


@native
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("default", ["5", '"x.csv"'])
@pytest.mark.parametrize("use", sorted(OUTPATH_NUMERIC))
def test_outpath_non_string_read_is_refused(use, default, form, sample_bars):
    read = "OutPath" if form == "bare" else "OutPath[1]"
    program = f"""
Inputs: OutPath({default});
{OUTPATH_NUMERIC[use].format(o=read)}
If MarketPosition = 1 and BarsSinceEntry >= 3 Then Sell next bar at market;
"""
    assert explain_native(program)
    with pytest.raises(AccelRefused):
        run_native(program, sample_bars, NQ)
    want = _oracle_outcome(program, sample_bars)
    assert _oracle_outcome(program, sample_bars, use_native=True) == want


@native
def test_outpath_numeric_compare_raises_like_python(sample_bars):
    program = "Inputs: OutPath(5);\nIf OutPath > 2 then Buy next bar at market;\n"
    with pytest.raises(TypeError) as want:
        run_oracle(program, sample_bars, NQ)
    with pytest.raises(TypeError) as got:
        run_oracle(program, sample_bars, NQ, use_native=True)
    assert str(got.value) == str(want.value)


# A string use of OutPath in a file sink is a no-op in both modes: still native.
@native
def test_outpath_file_path_stays_native(sample_bars):
    program = """
Inputs: OutPath("x.csv");
Vars: Line("");
Line = NumToStr(Close, 2) + ",";
If MarketPosition = 0 and Close > Close[1] Then Buy 1 contracts next bar at market;
If MarketPosition = 1 and BarsSinceEntry >= 3 Then Sell next bar at market;
Print(File(OutPath), Line);
Value1 = FileAppend(OutPath + "_log.csv", Line + NumToStr(Close, 2));
"""
    assert explain_native(program) == []
    got = run_oracle(program, sample_bars, NQ, use_native=True)
    assert got["native"]
    assert got["sha256"] == run_oracle(program, sample_bars, NQ)["sha256"]


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
