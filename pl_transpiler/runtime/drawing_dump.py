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

"""``powerbridge-drawing-dump`` v1: validator and canonical writer.

The contract is the one measured by the MultiCharts golden corpus
(``ground_truth/drawing/SCHEMA.md``). Canonical bytes are UTF-8, sorted
keys, separators ``(",", ":")``, and one trailing LF. Every failure is a
``DumpError`` naming the reason and the JSON path.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from typing import Any

SCHEMA = "powerbridge-drawing-dump"
VERSION = 1
PRODUCERS = {
    "mc-selfreport", "mc-probe", "mc", "pb-python", "pb-native", "expect",
}
TYPES = {"tl": 2, "arw": 1, "text": 1}  # anchors per type
OBJ_KEYS = {
    "type", "seq", "id", "created", "anchors", "color", "argb", "style",
    "size", "ext_left", "ext_right", "text", "hstyle", "vstyle", "font",
    "font_size", "bgcolor", "border", "direction", "deleted",
}
TOP_KEYS = {
    "schema", "version", "producer", "case_id", "tape", "objects",
    "final_count", "values", "unimplemented",
}
CASE_RE = re.compile(
    r"^g-(tl|arw|text|tool|cmt|mouse|colour|d2|x)-\d{3}-[a-z0-9-]+$"
)


class DumpError(ValueError):
    """A drawing dump broke the v1 contract."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _need(obj: dict[str, Any], keys: set[str], where: str) -> None:
    missing = sorted(keys - set(obj))
    if missing:
        raise DumpError(f"missing {missing[0]} at {where}")
    extra = sorted(set(obj) - keys)
    if extra:
        raise DumpError(f"unknown field {extra[0]} at {where}")


def _barkey(key, where: str, price: bool = False, tick: float = 0.25) -> None:
    if not isinstance(key, dict):
        raise DumpError(f"bar key not an object at {where}")
    keys = {"bn", "date", "time", "time_s"}
    if price:
        keys |= {"price", "price_ticks"}
    _need(key, keys, where)
    if price and key["bn"] is None:
        # An anchor that is not a tape bar, e.g. (0, 0) from an
        # out-of-history lookback: any non-negative date and time.
        if not _is_int(key["date"]) or key["date"] < 0:
            raise DumpError(f"date must be an int >= 0 at {where}.date")
        if not _is_int(key["time"]) or key["time"] < 0:
            raise DumpError(f"time must be an int >= 0 at {where}.time")
        if key["time_s"] != key["time"] * 100:
            raise DumpError(f"time_s must be time * 100 at {where}.time_s")
    elif not _is_int(key["bn"]) or key["bn"] < 1:
        raise DumpError(f"bn must be an int >= 1 at {where}.bn")
    elif not _is_int(key["date"]) or not 1000101 <= key["date"] <= 1991231:
        raise DumpError(f"date must be EL YYYMMDD at {where}.date")
    elif (not _is_int(key["time"]) or not 0 <= key["time"] <= 2359
            or key["time"] % 100 > 59):
        raise DumpError(f"time must be HHmm at {where}.time")
    elif (not _is_int(key["time_s"]) or key["time_s"] // 100 != key["time"]
            or key["time_s"] % 100 > 59):
        raise DumpError(f"time_s must be HHmmss matching time at {where}.time_s")
    if price:
        if not _num(key["price"]):
            raise DumpError(f"price must be a number at {where}.price")
        if (not _is_int(key["price_ticks"])
                or key["price_ticks"] != round(key["price"] / tick)):
            raise DumpError(
                f"price_ticks must equal round(price/tick) at {where}.price_ticks")


def _check_off_tape(dump: dict[str, Any]) -> None:
    """Reject a null-``bn`` anchor whose (date, time) is a known tape bar.

    The dump does not list every bar, so the known bars are the tape's first
    and last bar and every bar key in the dump that has a ``bn``.
    """
    tape = dump["tape"]
    on_tape = set()
    for bar in (tape["first"], tape["last"]):
        if isinstance(bar, list) and len(bar) == 2:
            on_tape.add((bar[0], bar[1]))
    nulls = []
    for index, obj in enumerate(dump["objects"]):
        keys = [(f"$.objects[{index}].{name}", obj[name])
                for name in ("created", "deleted") if obj[name] is not None]
        keys += [(f"$.objects[{index}].anchors[{anchor_i}]", anchor)
                 for anchor_i, anchor in enumerate(obj["anchors"])]
        for where, key in keys:
            if key["bn"] is None:
                nulls.append((where, key))
            else:
                on_tape.add((key["date"], key["time"]))
    for where, key in nulls:
        if (key["date"], key["time"]) in on_tape:
            raise DumpError(
                f"bn is null but date={key['date']} time={key['time']} is a "
                f"tape bar at {where}.bn")


def validate(dump: dict[str, Any]) -> dict[str, Any]:
    """Return ``dump`` if it is a v1 drawing dump; otherwise raise."""
    if not isinstance(dump, dict):
        raise DumpError("dump is not an object")
    if dump.get("schema") != SCHEMA:
        raise DumpError(f"unknown schema {dump.get('schema')!r}")
    if dump.get("version") != VERSION:
        raise DumpError(f"unknown version {dump.get('version')!r}")
    _need(dump, TOP_KEYS, "$")
    producer = dump["producer"]
    if not isinstance(producer, dict) or producer.get("kind") not in PRODUCERS:
        kind = producer.get("kind") if isinstance(producer, dict) else producer
        raise DumpError(f"unknown producer kind {kind!r}")
    if not isinstance(dump["case_id"], str) or not CASE_RE.match(dump["case_id"]):
        raise DumpError(f"case_id {dump['case_id']!r} not g-<family>-<NNN>-<slug>")
    tape = dump["tape"]
    _need(tape, {"name", "sha256", "bars", "first", "last", "tick"}, "$.tape")
    if not re.fullmatch(r"[0-9a-f]{64}", str(tape["sha256"])):
        raise DumpError("tape sha256 must be 64 hex chars at $.tape.sha256")
    if (not _is_int(tape["bars"]) or tape["bars"] < 1 or not _num(tape["tick"])
            or tape["tick"] <= 0):
        raise DumpError("tape bars/tick invalid at $.tape")
    tick = float(tape["tick"])
    if not isinstance(dump["objects"], list):
        raise DumpError("objects must be a list")
    live = {key: 0 for key in TYPES}
    for index, obj in enumerate(dump["objects"]):
        where = f"$.objects[{index}]"
        if not isinstance(obj, dict):
            raise DumpError(f"object not an object at {where}")
        if "seq" not in obj:
            raise DumpError(f"missing seq at {where}")
        _need(obj, OBJ_KEYS, where)
        if obj["type"] not in TYPES:
            raise DumpError(f"unknown type {obj['type']!r} at {where}.type")
        if not _is_int(obj["seq"]) or obj["seq"] != index + 1:
            raise DumpError(f"seq must be 1..n in order at {where}.seq")
        if obj["id"] is not None and not _is_int(obj["id"]):
            raise DumpError(f"id must be int or null at {where}.id")
        if obj["created"] is not None:
            _barkey(obj["created"], f"{where}.created")
        if (not isinstance(obj["anchors"], list)
                or len(obj["anchors"]) != TYPES[obj["type"]]):
            raise DumpError(
                f"{obj['type']} needs {TYPES[obj['type']]} anchor(s) "
                f"at {where}.anchors")
        for anchor_i, anchor in enumerate(obj["anchors"]):
            _barkey(anchor, f"{where}.anchors[{anchor_i}]", price=True, tick=tick)
        if not _is_int(obj["color"]) or not 0 <= obj["color"] <= 0xFFFFFFFF:
            raise DumpError(f"color must be an MC int (not hex text) at {where}.color")
        for key in ("style", "size", "hstyle", "vstyle", "font_size", "bgcolor"):
            if obj[key] is not None and not _is_int(obj[key]):
                raise DumpError(f"{key} must be int or null at {where}.{key}")
        for key in ("ext_left", "ext_right", "border"):
            if obj[key] is not None and not isinstance(obj[key], bool):
                raise DumpError(f"{key} must be bool or null at {where}.{key}")
        for key in ("text", "font", "argb"):
            if obj[key] is not None and not isinstance(obj[key], str):
                raise DumpError(f"{key} must be string or null at {where}.{key}")
        if obj["argb"] is not None and obj["argb"] != argb(obj["color"]):
            raise DumpError(f"argb {obj['argb']!r} disagrees with color at {where}.argb")
        if obj["direction"] not in (None, "up", "down"):
            raise DumpError(f"direction must be up/down/null at {where}.direction")
        if obj["deleted"] is not None:
            _barkey(obj["deleted"], f"{where}.deleted")
        else:
            live[obj["type"]] += 1
    _check_off_tape(dump)
    final = dump["final_count"]
    if (not isinstance(final, dict) or set(final) != set(TYPES)
            or any(not _is_int(value) for value in final.values())):
        raise DumpError("final_count must have int tl/arw/text")
    if final != live:
        raise DumpError(f"final_count {final} != live objects {live}")
    for index, value in enumerate(dump["values"]):
        _need(value, {"bn", "expr", "seq", "value"}, f"$.values[{index}]")
        if not _num(value["value"]):
            raise DumpError(f"value must be a number at $.values[{index}].value")
    if not isinstance(dump["unimplemented"], dict):
        raise DumpError("unimplemented must be an object")
    return dump


def argb(color: int) -> str:
    """Human-readable ``#AARRGGBB`` from MC's ``R + 256*G + 65536*B`` int."""
    red, green, blue = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
    return f"#FF{red:02X}{green:02X}{blue:02X}"


def canonical(dump: dict[str, Any]) -> bytes:
    """Canonical UTF-8 bytes of a validated dump."""
    return (
        json.dumps(dump, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def sha256(dump: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(dump)).hexdigest()


def blank_object(type_: str, seq: int) -> dict[str, Any]:
    obj: dict[str, Any] = {key: None for key in OBJ_KEYS}
    obj["type"] = type_
    obj["seq"] = seq
    obj["anchors"] = []
    return obj


def barkey(bn: int | None, date: int, time: int, price: float | None = None,
           tick: float = 0.25) -> dict[str, Any]:
    """One bar key. ``time_s`` is ``time`` as HHMMSS (seconds 0).

    ``bn`` is None for an anchor that is not a bar on the tape.

    ``price`` is stored as a float32 value, the precision chart prices have,
    so the Python and native paths write the same bytes.
    """
    key = {"bn": None if bn is None else int(bn), "date": int(date),
           "time": int(time), "time_s": int(time) * 100}
    if price is not None:
        value = struct.unpack("<f", struct.pack("<f", float(price)))[0]
        key["price"] = value
        key["price_ticks"] = round(value / tick)
    return key


def build_dump(objects: list[dict[str, Any]], *, case_id: str, tape_name: str, tape_sha256: str,
               n_bars: int, first, last, tick: float, producer_kind: str) -> dict[str, Any]:
    """Validate and return a v1 dump for ``objects`` in creation order.

    ``objects`` are recorder dicts (``blank_object`` shape, ``seq`` ignored).
    ``seq`` and ``argb`` are assigned here so two runs of the same tape produce
    the same bytes.
    """
    dumped = []
    for index, obj in enumerate(objects, 1):
        item = dict(obj)
        item["seq"] = index
        color = item.get("color")
        item["argb"] = argb(color) if isinstance(color, int) else None
        dumped.append(item)
    final = {
        key: sum(1 for obj in dumped if obj["type"] == key and obj["deleted"] is None)
        for key in TYPES
    }
    dump = {
        "schema": SCHEMA,
        "version": VERSION,
        "producer": {
            "kind": producer_kind,
            "mc_build": None,
            "pb_commit": None,
            "run_id": None,
        },
        "case_id": case_id,
        "tape": {
            "name": tape_name,
            "sha256": tape_sha256,
            "bars": int(n_bars),
            "first": [int(first[0]), int(first[1])],
            "last": [int(last[0]), int(last[1])],
            "tick": float(tick),
        },
        "objects": dumped,
        "final_count": final,
        "values": [],
        "unimplemented": {},
    }
    return validate(dump)
