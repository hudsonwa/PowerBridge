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

"""Drawing dump v1. Opt-in: a run that does not pass drawings=True keeps the
previous return shape and the same trade digest.

Every case under ground_truth/drawing/ runs on synthetic bars here. The
comparison against the MultiCharts readbacks runs only when your own copy of
that corpus is in place. Colour constants, Tool_* styles and creation
defaults are the measured MultiCharts values.
"""
import gzip
import json
import os
import struct

import pytest

from pl_transpiler.errors import UnimplementedKeywordError
from pl_transpiler.parser_tables import COLOR_CONSTANTS, STYLE_CONSTANTS
from pl_transpiler.runtime._rt_core import UnimplementedBuiltinError, set_strict_mode
from pl_transpiler.runtime._rt_drawing import DrawingRecorder
from pl_transpiler.runtime.drawing_dump import (
    DumpError, barkey, canonical, validate,
)
from pl_transpiler.runtime.pl_runtime import (
    pl_gradientcolor, pl_legacycolortorgb, pl_rgb, pl_tl_getbeginval,
    pl_tl_getcolor, pl_tl_getvalue, pl_tl_new, pl_tl_setcolor,
)
from pl_transpiler.tools.pl_run import run_el
from pl_transpiler import transpile
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_oracle import run_oracle
from pl_transpiler.tools.pl_run import load_bars_csv
from tests._drawing_diff import (
    CORPUS, CORPUS_SKIP, TAPES, DiffRefused, case_ids, diff,
    drawing_corpus_present,
)

ES = get_config("ES")

_BARS = [
    {"date": 1260105, "time": 1000, "open": 10, "high": 12, "low": 9,
     "close": 11, "volume": 1},
    {"date": 1260105, "time": 1001, "open": 11, "high": 13, "low": 10,
     "close": 12, "volume": 1},
    {"date": 1260105, "time": 1002, "open": 12, "high": 14, "low": 8,
     "close": 13, "volume": 1},
]
_EL = (
    "Vars: id(-1);\n"
    "If Time = 1002 Then id = TL_New(Date[2], Time[2], Low[2], Date, Time, High);\n"
)
_TAPE = {"name": "synth", "sha256": "a" * 64, "tick": 0.25}


def _run(**kwargs):
    return run_oracle(
        _EL, _BARS, ES, drawing_case_id="g-x-001-synth", drawing_tape=_TAPE,
        **kwargs)


def test_drawings_off_keeps_the_return_shape_and_digest():
    plain = run_oracle(_EL, _BARS, ES)
    recorded = _run(drawings=True)
    assert "drawings" not in plain
    assert plain["sha256"] == recorded["sha256"]
    assert plain["text"] == recorded["text"]


def test_dump_anchors_ids_and_repeat_bytes():
    first = _run(drawings=True)
    second = _run(drawings=True)
    dump = validate(first["drawings"])
    assert canonical(first["drawings"]) == canonical(second["drawings"])
    assert dump["final_count"] == {"tl": 1, "arw": 0, "text": 0}
    obj = dump["objects"][0]
    assert obj["seq"] == 1 and obj["id"] == 1 and obj["type"] == "tl"
    assert obj["anchors"][0]["bn"] == 1
    assert obj["anchors"][0]["time"] == 1000
    assert obj["anchors"][0]["price_ticks"] == round(9 / 0.25)
    assert obj["anchors"][1]["bn"] == 3
    assert obj["anchors"][1]["price_ticks"] == round(14 / 0.25)
    # g-tl-001-new measured default.
    assert obj["color"] == 36095
    assert obj["style"] == 1 and obj["size"] == 0
    assert obj["ext_left"] is False and obj["ext_right"] is False


def test_anchor_price_is_stored_as_float32():
    """Both paths write float32 prices, so their dumps are byte-identical."""
    key = barkey(1, 1260102, 1000, 14957.45)
    assert key["price"] == struct.unpack("<f", struct.pack("<f", 14957.45))[0]
    assert key["price_ticks"] == 59830
    assert barkey(1, 1260102, 1000, 10.125)["price_ticks"] == 40


def test_unknown_version_is_refused():
    dump = _run(drawings=True)["drawings"]
    dump["version"] = 99
    with pytest.raises(DumpError, match="unknown version"):
        validate(dump)
    with pytest.raises(DiffRefused, match="unknown schema"):
        diff({"schema": "nope"}, dump)


def test_missing_case_id_is_refused():
    with pytest.raises(ValueError, match="drawing_case_id"):
        run_oracle(_EL, _BARS, ES, drawings=True)


def _corpus_bars(name):
    path = os.path.join(TAPES, name)
    cached = _BAR_CACHE.get(path)
    if cached is None:
        cached = _BAR_CACHE[path] = _load_gz_csv(path)
    return cached


_BAR_CACHE = {}


def _load_gz_csv(path):
    """load_bars_csv does not open gzip, so decompress to a temporary file."""
    import tempfile
    with gzip.open(path, "rb") as fh:
        raw = fh.read()
    handle = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    handle.write(raw)
    handle.close()
    try:
        return load_bars_csv(handle.name)
    finally:
        os.unlink(handle.name)


def _case(case_id):
    path = os.path.join(CORPUS, case_id, "case.json")
    return json.loads(open(path, encoding="utf-8").read())


def _source(case_id):
    path = os.path.join(CORPUS, case_id, "source.el")
    return open(path, encoding="utf-8").read()


def _synthetic_bars(days=2, seed=7):
    """Deterministic 1-minute bars, 09:30-14:59, prices on a 0.25 tick grid.

    A small linear congruential generator keeps the series identical on
    every platform and Python version.
    """
    state = seed
    price = 4000.0
    bars = []
    for day in range(days):
        date = 1260105 + day
        for minute in range(9 * 60 + 30, 15 * 60):
            state = (state * 1103515245 + 12345) % 2 ** 31
            step = (state % 9 - 4) * 0.25
            open_ = price
            close = price + step
            high = max(open_, close) + 0.25 * (state % 3)
            low = min(open_, close) - 0.25 * (state % 2)
            bars.append({
                "date": date, "time": (minute // 60) * 100 + minute % 60,
                "open": open_, "high": high, "low": low, "close": close,
                "volume": 1 + state % 50,
            })
            price = close
    return bars


def _synthetic_data2(bars):
    return [dict(bar, open=bar["open"] - 2, high=bar["high"] - 2,
                 low=bar["low"] - 2, close=bar["close"] - 2) for bar in bars]


def _run_case(case_id, bars, data2, tape, **kwargs):
    if data2 is not None:
        kwargs["data2_bars"] = data2
    return run_oracle(
        _source(case_id), bars, ES, max_bars_back=50, drawings=True,
        drawing_case_id=case_id, drawing_tape=tape, **kwargs)


def _synthetic_run(case_id):
    bars = _synthetic_bars()
    data2 = _synthetic_data2(bars) if _case(case_id).get("data2") else None
    return _run_case(case_id, bars, data2, _TAPE)


def _case_params():
    params = []
    for case_id in case_ids():
        reason = _case(case_id)["xfail"].get("pb-python")
        if reason:
            params.append(pytest.param(case_id, marks=pytest.mark.xfail(
                strict=True, reason=reason)))
        else:
            params.append(case_id)
    return params


_TYPES = {"tl": "tl", "arw": "arw", "text": "text"}


@pytest.mark.parametrize("case_id", _case_params())
def test_case_runs_on_synthetic_bars(case_id):
    """Every case records a valid dump with stable bytes, on bars anyone has."""
    first = _synthetic_run(case_id)
    second = _synthetic_run(case_id)
    dump = validate(first["drawings"])
    assert canonical(first["drawings"]) == canonical(second["drawings"])
    assert first["sha256"] == second["sha256"]
    assert dump["objects"], "the case drew nothing on the synthetic bars"
    family = case_id.split("-")[1]
    if family in _TYPES:
        assert {obj["type"] for obj in dump["objects"]} == {_TYPES[family]}
    assert sum(dump["final_count"].values()) == len(dump["objects"])
    assert diff(first["drawings"], second["drawings"], compare_ids=True) == []
    _check_schema(_SCHEMA, json.loads(canonical(first["drawings"])), "$")


def test_tl_anchors_on_synthetic_bars():
    """g-tl-001-new: one TL per on-the-hour bar from 10:00, Low[29] to High."""
    bars = _synthetic_bars()
    dump = _synthetic_run("g-tl-001-new")["drawings"]
    hours = [index for index, bar in enumerate(bars)
             if bar["time"] % 100 == 0 and bar["time"] >= 1000 and index >= 50]
    assert [obj["anchors"][1]["bn"] for obj in dump["objects"]] == [
        index + 1 for index in hours]
    for obj, index in zip(dump["objects"], hours):
        begin, end = obj["anchors"]
        assert begin["bn"] == index + 1 - 29
        assert begin["price_ticks"] == round(bars[index - 29]["low"] / 0.25)
        assert end["price_ticks"] == round(bars[index]["high"] / 0.25)


_SCHEMA = json.loads(open(os.path.join(
    CORPUS, "schema", "drawing-dump.v1.json"), encoding="utf-8").read())
_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None), "integer": int, "number": (int, float),
}


def _type_ok(value, name):
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES[name])


def _check_schema(node, value, path):
    """The subset of JSON Schema that drawing-dump.v1.json uses."""
    import re
    if "$ref" in node:
        node = _SCHEMA["$defs"][node["$ref"].rsplit("/", 1)[1]]
    if "oneOf" in node:
        matches = 0
        for option in node["oneOf"]:
            try:
                _check_schema(option, value, path)
            except AssertionError:
                continue
            matches += 1
        assert matches == 1, f"{path}: {matches} oneOf branches match"
        return
    kind = node.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        assert any(_type_ok(value, name) for name in kinds), f"{path}: {value!r} is not {kind}"
    if "const" in node:
        assert value == node["const"], path
    if "enum" in node:
        assert value in node["enum"], f"{path}: {value!r}"
    if "pattern" in node and isinstance(value, str):
        assert re.search(node["pattern"], value), f"{path}: {value!r}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        assert value >= node.get("minimum", value), path
        assert value <= node.get("maximum", value), path
        if "exclusiveMinimum" in node:
            assert value > node["exclusiveMinimum"], path
    if isinstance(value, dict):
        for key in node.get("required", ()):
            assert key in value, f"{path}: missing {key}"
        props = node.get("properties", {})
        if node.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{path}: unexpected {sorted(extra)}"
        for key, item in value.items():
            if key in props:
                _check_schema(props[key], item, f"{path}.{key}")
    if isinstance(value, list):
        assert len(value) >= node.get("minItems", 0), path
        assert len(value) <= node.get("maxItems", len(value)), path
        if "items" in node:
            for index, item in enumerate(value):
                _check_schema(node["items"], item, f"{path}[{index}]")


def test_schema_check_rejects_a_broken_dump():
    dump = json.loads(canonical(_run(drawings=True)["drawings"]))
    _check_schema(_SCHEMA, dump, "$")
    dump["objects"][0]["type"] = "ellipse"
    with pytest.raises(AssertionError, match="ellipse"):
        _check_schema(_SCHEMA, dump, "$")


# The tests below compare against MultiCharts readbacks that are not in the
# repository. They skip unless your own copy is in place.
needs_corpus = pytest.mark.skipif(
    not drawing_corpus_present(), reason=CORPUS_SKIP)


def _mc_dump(case_id):
    path = os.path.join(CORPUS, case_id, "mc", "drawings.json")
    return json.loads(open(path, encoding="utf-8").read())


def _digest_line(case_id):
    path = os.path.join(CORPUS, case_id, "mc", "trades.digest")
    return open(path, encoding="utf-8").read().split()[0]


def _corpus_run(case_id):
    """Run a case on the corpus bars, under the tape identity MultiCharts saw."""
    tape = dict(_mc_dump(case_id)["tape"])
    tape.setdefault("tick", 0.25)
    data2 = None
    if _case(case_id).get("data2"):
        data2 = _corpus_bars("data2.csv.gz")
    return _run_case(case_id, _corpus_bars("data1.csv.gz"), data2, tape)


def _corpus_params():
    params = []
    for case_id in case_ids():
        reason = _case(case_id)["xfail"].get("pb-python")
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        params.append(pytest.param(case_id, marks=marks))
    return params


@needs_corpus
@pytest.mark.parametrize("case_id", _corpus_params())
def test_corpus_full_drawing_match(case_id):
    result = _corpus_run(case_id)
    assert result["sha256"] == _digest_line(case_id)
    assert diff(_mc_dump(case_id), result["drawings"]) == []


@needs_corpus
def test_corpus_tl_dump_is_stable_across_runs():
    first = _corpus_run("g-tl-001-new")
    second = _corpus_run("g-tl-001-new")
    assert canonical(first["drawings"]) == canonical(second["drawings"])
    assert first["drawings"]["final_count"] == _mc_dump("g-tl-001-new")["final_count"]


def _native_params():
    params = []
    for case_id in case_ids():
        reason = _case(case_id)["xfail"].get("pb-native")
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        params.append(pytest.param(case_id, marks=marks))
    return params


def _needs_native():
    from pl_transpiler.runtime.accel import accel_available
    if not accel_available():
        pytest.skip("native kernel is not installed")


def _assert_native_matches(case_id, bars, data2, tape):
    python = _run_case(case_id, bars, data2, tape)
    native = _run_case(case_id, bars, data2, tape, use_native=True)
    assert native["native"] is True, native.get("native_fallback_reason")
    assert native["sha256"] == python["sha256"]
    assert native["drawings"]["producer"]["kind"] == "pb-native"
    native["drawings"]["producer"] = python["drawings"]["producer"]
    assert canonical(native["drawings"]) == canonical(python["drawings"])
    return native


@pytest.mark.parametrize("case_id", _native_params())
def test_native_drawings_match_python_on_synthetic_bars(case_id):
    """The kernel records the same objects as Python. producer.kind is the
    only envelope difference."""
    _needs_native()
    bars = _synthetic_bars()
    data2 = _synthetic_data2(bars) if _case(case_id).get("data2") else None
    _assert_native_matches(case_id, bars, data2, _TAPE)


def test_native_drawings_match_python_off_the_float32_grid():
    """Prices like 4000.01 are not exact in float32; both paths must round
    them the same way."""
    _needs_native()
    bars = [dict(bar, open=bar["open"] + 0.01, high=bar["high"] + 0.01,
                 low=bar["low"] + 0.01, close=bar["close"] + 0.01)
            for bar in _synthetic_bars()]
    native = _assert_native_matches("g-tl-001-new", bars, None, _TAPE)
    assert native["drawings"]["objects"]


@needs_corpus
@pytest.mark.parametrize("case_id", _native_params())
def test_native_corpus_drawings_match_python(case_id):
    _needs_native()
    tape = dict(_mc_dump(case_id)["tape"])
    tape.setdefault("tick", 0.25)
    data2 = _corpus_bars("data2.csv.gz") if _case(case_id).get("data2") else None
    native = _assert_native_matches(
        case_id, _corpus_bars("data1.csv.gz"), data2, tape)
    assert diff(_mc_dump(case_id), native["drawings"]) == []


def test_measured_colour_words_and_functions():
    """Values cited by g-tool-001-dashed and g-colour-001-hours. No guessed palette."""
    assert COLOR_CONSTANTS["red"] == 255
    assert COLOR_CONSTANTS["blue"] == 16711680
    assert COLOR_CONSTANTS["darkyellow"] == 32896
    assert COLOR_CONSTANTS["darkgray"] == 8421504
    assert COLOR_CONSTANTS["darkbrown"] == 2179941
    assert STYLE_CONSTANTS == {
        "tool_solid": 1, "tool_dashed": 2, "tool_dotted": 3,
        "tool_dashed2": 4, "tool_dashed3": 5,
    }
    assert pl_rgb(10, 20, 30) == 1971210
    assert pl_legacycolortorgb(14) == 32896
    assert pl_gradientcolor(5, 0, 10, 255, 16711680) == 8323200
    with pytest.raises(UnimplementedBuiltinError, match="not measured"):
        pl_legacycolortorgb(1)
    with pytest.raises(UnimplementedBuiltinError, match="Min = Max"):
        pl_gradientcolor(1, 1, 1, 255, 0)
    with pytest.raises(UnimplementedKeywordError):
        transpile("Vars: x(0);\nx = Tool_DashDot;")


def test_tl_getvalue_refuses_to_invent_a_price():
    """Trading off TL_GetValue must stop. There is no measured interpolation."""
    src = (
        "Vars: id(0);\n"
        "id = TL_New(Date, Time, Low, Date, Time, High);\n"
        "If Close > TL_GetValue(id, Date, Time) Then Buy next bar at market;\n"
    )
    with pytest.raises(UnimplementedBuiltinError, match="TL_GetValue"):
        run_el(src, _BARS, ES)
    set_strict_mode(False)
    try:
        with pytest.raises(UnimplementedBuiltinError, match="TL_GetValue"):
            run_el(src, _BARS, ES)
    finally:
        set_strict_mode(True)


def test_getter_reads_recorded_default_and_set():
    dates = [bar["date"] for bar in _BARS]
    times = [bar["time"] for bar in _BARS]
    rec = DrawingRecorder(dates, times, tick=0.25)
    rec.set_bar(2)
    with rec:
        oid = pl_tl_new(1260105, 1000, 9, 1260105, 1002, 14)
        assert pl_tl_getcolor(oid) == 36095
        assert pl_tl_getbeginval(oid) == 9
        pl_tl_setcolor(oid, 255)
        assert pl_tl_getcolor(oid) == 255
        with pytest.raises(UnimplementedBuiltinError, match="TL_GetValue"):
            pl_tl_getvalue(oid, 1260105, 1002)
    with pytest.raises(UnimplementedBuiltinError, match="drawings=True"):
        pl_tl_getcolor(1)
