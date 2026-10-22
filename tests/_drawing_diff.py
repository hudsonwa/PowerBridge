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

"""Drawing-dump diff. A is the reference, B is the candidate.

Floats use the engine's float32 rule, ``|a-b| <= 1e-6 + 1e-6*|b|``. Counts,
bar keys, price ticks, ints, bools and text are exact. An unknown schema or
a tape sha mismatch is a :class:`DiffRefused`, not a difference list.
"""
from __future__ import annotations

import os

from pl_transpiler.runtime.drawing_dump import DumpError, validate

RTOL = ATOL = 1e-6
EXACT = (
    "type", "color", "style", "size", "ext_left", "ext_right", "direction",
    "hstyle", "vstyle", "border", "font_size", "bgcolor", "text", "font",
    "deleted",
)
BAR = ("bn", "date", "time", "time_s")


class DiffRefused(ValueError):
    """A dump could not be compared (invalid, or the contract does not apply)."""


def close(left: float, right: float) -> bool:
    return abs(left - right) <= ATOL + RTOL * abs(right)


def diff(reference: dict, candidate: dict, *, compare_ids: bool = False) -> list:
    """List of named differences. Empty means the dumps agree."""
    for name, dump in (("A", reference), ("B", candidate)):
        try:
            validate(dump)
        except DumpError as exc:
            raise DiffRefused(f"{name}: {exc}") from None
    out = []
    if reference["tape"]["sha256"] != candidate["tape"]["sha256"]:
        out.append(
            "tape sha mismatch: "
            f"{reference['tape']['sha256'][:12]} != {candidate['tape']['sha256'][:12]}"
        )
    if reference["final_count"] != candidate["final_count"]:
        out.append(
            f"final_count: {reference['final_count']} != {candidate['final_count']}")
    left, right = reference["objects"], candidate["objects"]
    if len(left) != len(right):
        which = "extra" if len(right) > len(left) else "missing"
        out.append(f"object count: {len(left)} != {len(right)} ({which} object)")
    for src, dst in zip(left, right):
        label = f"seq {src['seq']}"
        for key in EXACT:
            if src[key] != dst[key] or type(src[key]) is not type(dst[key]):
                out.append(f"{label} {key}: {src[key]!r} != {dst[key]!r}")
        if compare_ids and src["id"] != dst["id"]:
            out.append(f"{label} id: {src['id']!r} != {dst['id']!r}")
        if src["created"] is not None and dst["created"] is not None:
            for key in BAR:
                if src["created"][key] != dst["created"][key]:
                    out.append(
                        f"{label} created.{key}: "
                        f"{src['created'][key]} != {dst['created'][key]}")
        for index, (anchor_a, anchor_b) in enumerate(
                zip(src["anchors"], dst["anchors"])):
            for key in BAR + ("price_ticks",):
                if anchor_a[key] != anchor_b[key]:
                    out.append(
                        f"{label} anchors[{index}].{key}: "
                        f"{anchor_a[key]} != {anchor_b[key]}")
    values_a = {
        (row["seq"], row["bn"], row["expr"]): row["value"]
        for row in reference["values"]
    }
    values_b = {
        (row["seq"], row["bn"], row["expr"]): row["value"]
        for row in candidate["values"]
    }
    for key in sorted(set(values_a) | set(values_b)):
        if key not in values_a or key not in values_b:
            out.append(f"values {key}: present on one side only")
        elif not close(values_b[key], values_a[key]):
            out.append(
                f"values {key}: {values_a[key]!r} != {values_b[key]!r} (float32 rule)")
    if reference["unimplemented"] != candidate["unimplemented"]:
        out.append(
            f"unimplemented: {reference['unimplemented']} "
            f"!= {candidate['unimplemented']}")
    return out


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, "ground_truth", "drawing")
TAPES = os.path.join(CORPUS, "tapes")
CORPUS_SKIP = ("needs the MultiCharts drawing corpus (not redistributed); "
               "see docs/DRAWING_DUMP.md")


def case_ids():
    """The g-* case folders, sorted."""
    return sorted(
        name for name in os.listdir(CORPUS)
        if name.startswith("g-") and os.path.isdir(os.path.join(CORPUS, name)))


def drawing_corpus_present():
    """True when the bars and every case's MultiCharts readback are on disk."""
    if not os.path.isfile(os.path.join(TAPES, "data1.csv.gz")):
        return False
    return all(
        os.path.isfile(os.path.join(CORPUS, case, "mc", name))
        for case in case_ids() for name in ("drawings.json", "trades.digest"))
