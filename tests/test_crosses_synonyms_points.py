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

"""`crosses over` / `crosses under` synonyms and the `N Point(s)` price unit.

`crosses over` must transpile and trade exactly like `crosses above`, and
`crosses under` like `crosses below`. `N Point` is N minimum price moves
(N * MinMove / PriceScale; NQ: 0.25), so `+ 2 Points` must trade like `+ 0.5`.
`2 Ticks` is not supported and the error names `ticks`, not `stop`.

Stdlib + pytest only, synthetic bars, no captures.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile                                  # noqa: E402
from pl_transpiler.lexer import PLSyntaxError, tokenise               # noqa: E402
from pl_transpiler.parser import parse                                # noqa: E402
from pl_transpiler.runtime.accel import AccelRefused, run_native     # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config       # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle                 # noqa: E402
from tests.skip_messages import ACCEL_SKIP                         # noqa: E402
from tests.test_point_stops import BARS                              # noqa: E402

NQ = get_config("NQ")

CROSS = """
Vars: Avg(0) ;
Avg = Average(Close, 10) ;
If Close crosses {up} Avg Then Buy ("LE") next bar at market ;
If Close crosses {down} Avg Then SellShort ("SE") next bar at market ;
"""


def _strip_positions(node):
    """Drop source-position metadata (`_line`, `_col`, ...): `over` is shorter than `above`."""
    if isinstance(node, dict):
        return {k: _strip_positions(v) for k, v in node.items() if not k.startswith("_")}
    if isinstance(node, list):
        return [_strip_positions(v) for v in node]
    return node


@pytest.mark.parametrize("synonym,canonical", [("over", "above"), ("under", "below")])
def test_crosses_synonym_parses_like_canonical(synonym, canonical):
    src = "If Close crosses {} Average(Close, 10) Then Buy next bar at market ;"
    ast = _strip_positions(parse(*tokenise(src.format(synonym))))
    assert ast == _strip_positions(parse(*tokenise(src.format(canonical))))
    assert ast["body"][0]["cond"]["name"] == "crosses" + canonical


def test_crosses_over_under_same_code_and_trades():
    syn = CROSS.format(up="over", down="under")
    canon = CROSS.format(up="above", down="below")
    assert transpile(syn) == transpile(canon)
    records = run_oracle(syn, BARS, NQ)["records"]
    assert records, "the crossover strategy never traded on the synthetic tape"
    assert records == run_oracle(canon, BARS, NQ)["records"]


def _stop_el(offset):
    return f"""
If MarketPosition = 0 Then Buy ("LE") next bar at Highest(High, 5) + {offset} Stop ;
If BarsSinceEntry > 10 Then Sell ("LX") next bar at market ;
"""


@pytest.mark.parametrize("unit", ["Point", "Points", "point"])
def test_points_are_minmove_over_pricescale(unit):
    # NQ: MinMove 25 / PriceScale 100 = 0.25, so 2 Points = 0.5.
    records = run_oracle(_stop_el(f"2 {unit}"), BARS, NQ)["records"]
    assert records, "the stop entry never filled on the synthetic tape"
    assert records == run_oracle(_stop_el("0.5"), BARS, NQ)["records"]
    assert records != run_oracle(_stop_el("0"), BARS, NQ)["records"]


def test_points_native_kernel_refuses_loudly():
    pytest.importorskip("pl_transpiler._accel", reason=ACCEL_SKIP)
    # The native kernel has no MinMove/PriceScale value; it must refuse, not guess.
    with pytest.raises(AccelRefused):
        run_native(_stop_el("1 Point"), BARS, NQ)


def test_ticks_error_names_the_offending_word():
    with pytest.raises(PLSyntaxError) as exc:
        transpile("Buy next bar at Highest(High, 5) + 2 Ticks Stop ;")
    msg = str(exc.value)
    assert "'ticks'" in msg
    assert "'stop'" not in msg
