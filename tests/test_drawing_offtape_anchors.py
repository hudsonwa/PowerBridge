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

"""Drawing anchors that are not bars on the tape (#157).

``TL_New(Date[1], Time[1], ...)`` on the first bar anchors at (0, 0), and a
session-end time is generally not a bar. Both paths keep the raw (date, time)
with ``bn`` null, and the native dump matches Python's byte for byte.
"""
import pytest

from pl_transpiler.runtime.drawing_dump import DumpError, barkey, canonical, validate
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_oracle import run_oracle

ES = get_config("ES")
_TAPE = {"name": "synth", "sha256": "b" * 64, "tick": 0.25}


def _bars(count=6):
    bars = []
    for index in range(count):
        base = 100 + index * (1 if index % 3 else -2)
        bars.append({"date": 1260105, "time": 1000 + index * 5,
                     "open": base, "high": base + 2, "low": base - 2,
                     "close": base + (1 if index % 2 else -1), "volume": 1})
    return bars


PROGRAMS = {
    # Date[1]/Time[1] are (0, 0) on bar 1; 1615 is a session end, not a bar.
    "out_of_history_and_session_end": (
        "Vars: id(-1);\n"
        "id = TL_New(Date[1], Time[1], Low, Date, 1615, High);\n"
        "If Close > Open Then Buy next bar at market;\n"
        "If Close < Open Then Sell next bar at market;\n"
    ),
    "session_end_time": (
        "Vars: id(-1);\n"
        "id = TL_New(Date[1], Time[1], Low, Date, SessionEndTime(0, 1), High);\n"
        "If Close > Open Then Buy next bar at market;\n"
    ),
    # 1002 falls between the 1000 and 1005 bars.
    "between_bars": (
        "Vars: id(-1);\n"
        "If BarNumber = 2 Then id = TL_New(Date, 1002, Low, Date, Time, High);\n"
        "If BarNumber = 3 Then id = Text_New(Date, 1003, Close, \"mid\");\n"
        "If Close > Open Then Buy next bar at market;\n"
    ),
    "future_date": (
        "Vars: id(-1);\n"
        "If BarNumber = 1 Then id = Arw_New(1301231, 930, High, false);\n"
        "If BarNumber = 1 Then id = TL_New(Date, Time, Low, 1301231, 930, High);\n"
        "If Close < Open Then Sell next bar at market;\n"
    ),
}

# Anchors on later bars: 1010 is bar 3 of the tape, 1012 is not a bar.
FUTURE = {
    "future_anchor_on_the_tape": (
        "Vars: id(-1);\n"
        "If BarNumber = 1 Then id = Arw_New(Date, 1010, Low, true);\n"
        "If BarNumber = 1 Then id = TL_New(Date, Time, Low, Date, 1020, High);\n"
        "If Close > Open Then Buy next bar at market;\n"
    ),
    "future_anchor_not_on_the_tape": (
        "Vars: id(-1);\n"
        "If BarNumber = 1 Then id = Arw_New(Date, 1012, Low, true);\n"
        "If Close > Open Then Buy next bar at market;\n"
    ),
}

# The native kernel refuses TL_SetBegin/TL_SetEnd and the getters at compile
# time, so this runs on Python; the anchors still need no tape bar.
SETTERS = (
    "Vars: id(-1), d(0), t(0);\n"
    "If BarNumber = 1 Then id = TL_New(Date, Time, Low, Date, Time, High);\n"
    "If BarNumber = 1 Then begin\n"
    " TL_SetBegin(id, Date[1], Time[1], Low);\n"
    " TL_SetEnd(id, Date, 1615, High);\n"
    " d = TL_GetBeginDate(id);\n"
    " t = TL_GetEndTime(id);\n"
    "end;\n"
    "If BarNumber = 3 and d = 0 and t = 1615 Then Buy next bar at market;\n"
)


def _run(src, **kwargs):
    return run_oracle(src, _bars(), ES, drawings=True,
                      drawing_case_id="g-x-157-anchor", drawing_tape=_TAPE,
                      **kwargs, max_bars_back=0)


def _off_tape(dump):
    return [a for obj in dump["objects"] for a in obj["anchors"]
            if a["bn"] is None]


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_python_keeps_off_tape_anchors(name):
    dump = validate(_run(PROGRAMS[name])["drawings"])
    assert _off_tape(dump)


def test_out_of_history_anchor_is_zero_zero():
    dump = validate(_run(PROGRAMS["out_of_history_and_session_end"])["drawings"])
    first = dump["objects"][0]["anchors"]
    assert first[0] == {"bn": None, "date": 0, "time": 0, "time_s": 0,
                        "price": first[0]["price"],
                        "price_ticks": first[0]["price_ticks"]}
    assert first[1]["bn"] is None and first[1]["time_s"] == 161500


def test_setters_and_getters_accept_off_tape_anchors():
    result = _run(SETTERS)
    dump = validate(result["drawings"])
    begin, end = dump["objects"][0]["anchors"]
    assert (begin["bn"], begin["date"], begin["time"]) == (None, 0, 0)
    assert (end["bn"], end["time"]) == (None, 1615)
    # The getters read the raw anchor, so the buy fires.
    assert result["text"] != _run(SETTERS.replace("t = 1615", "t = 1"))["text"]


def test_validator_still_checks_off_tape_anchors():
    obj = barkey(None, 0, 0, 1.0)
    assert obj["bn"] is None
    dump = _run(PROGRAMS["between_bars"])["drawings"]
    dump["objects"][0]["anchors"][0]["time_s"] = 1
    with pytest.raises(DumpError, match="time_s"):
        validate(dump)
    dump = _run(PROGRAMS["between_bars"])["drawings"]
    dump["objects"][0]["created"]["bn"] = None
    with pytest.raises(DumpError, match="bn"):
        validate(dump)


def test_future_anchor_on_the_tape_gets_its_bn():
    dump = validate(_run(FUTURE["future_anchor_on_the_tape"])["drawings"])
    arrow, line = dump["objects"]
    assert (arrow["anchors"][0]["bn"], arrow["anchors"][0]["time"]) == (3, 1010)
    assert (line["anchors"][1]["bn"], line["anchors"][1]["time"]) == (5, 1020)
    dump = validate(_run(FUTURE["future_anchor_not_on_the_tape"])["drawings"])
    assert dump["objects"][0]["anchors"][0]["bn"] is None


def test_validator_rejects_null_bn_on_a_known_tape_bar():
    dump = _run(FUTURE["future_anchor_on_the_tape"])["drawings"]
    dump["objects"][1]["anchors"][0]["bn"] = None
    with pytest.raises(DumpError, match="is a tape bar"):
        validate(dump)
    dump = _run(FUTURE["future_anchor_on_the_tape"])["drawings"]
    last = dump["tape"]["last"]
    anchor = dump["objects"][0]["anchors"][0]
    anchor.update(bn=None, date=last[0], time=last[1], time_s=last[1] * 100)
    with pytest.raises(DumpError, match="is a tape bar"):
        validate(dump)


def _needs_native():
    from pl_transpiler.runtime.accel import accel_available
    if not accel_available():
        pytest.skip("native kernel is not installed")


@pytest.mark.needs_native
@pytest.mark.parametrize("name", sorted(PROGRAMS) + sorted(FUTURE))
def test_native_matches_python(name):
    _needs_native()
    src = {**PROGRAMS, **FUTURE}[name]
    python = _run(src)
    native = _run(src, use_native=True)
    assert native["native"] is True, native.get("native_fallback_reason")
    assert native["sha256"] == python["sha256"]
    assert native["drawings"]["producer"]["kind"] == "pb-native"
    native["drawings"]["producer"] = python["drawings"]["producer"]
    assert canonical(native["drawings"]) == canonical(python["drawings"])


@pytest.mark.needs_native
def test_native_plain_run_does_not_refuse_on_bar_one():
    _needs_native()
    src = PROGRAMS["out_of_history_and_session_end"]
    python = run_oracle(src, _bars(), ES, max_bars_back=0)
    native = run_oracle(src, _bars(), ES, use_native=True, max_bars_back=0)
    assert native["native"] is True, native.get("native_fallback_reason")
    assert native["sha256"] == python["sha256"]


@pytest.mark.needs_native
def test_setters_fall_back_with_the_same_digest():
    _needs_native()
    python = _run(SETTERS)
    native = _run(SETTERS, use_native=True)
    assert native["sha256"] == python["sha256"]
    native["drawings"]["producer"] = python["drawings"]["producer"]
    assert canonical(native["drawings"]) == canonical(python["drawings"])
