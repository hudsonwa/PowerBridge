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

"""test_el_abbreviations.py — standard EL shorthand.

O/H/L/C/V/I abbreviate Open/High/Low/Close/Volume/OpenInt (with lookback and as
function arguments); a declared input/variable of the same name shadows the
abbreviation (documented behaviour). `Sell Short` and `Buy To Cover` are the
documented two-word forms of SellShort and BuyToCover. A strategy written with
the shorthand must give the same digest as its long-form twin, on both the
native and the Python path.

Stdlib only, no captures.
"""
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile                                       # noqa: E402
from pl_transpiler.lexer import tokenise                                 # noqa: E402
from pl_transpiler.parser import parse                                   # noqa: E402
import pytest                                                            # noqa: E402

from tests.skip_messages import ACCEL_SKIP                               # noqa: E402
from pl_transpiler.runtime.accel import accel_available                  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config           # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle                     # noqa: E402

NQ = get_config("NQ")


def _bars(n=120):
    out = []
    for i in range(n):
        mid = 100.0 + 8.0 * math.sin(i / 7.0) + 0.05 * i
        out.append({"date": 1240101 + i, "time": 1000, "open": mid - 0.25,
                    "high": mid + 1.0, "low": mid - 1.0, "close": mid + 0.25,
                    "volume": 100.0 + i})
    return out


LONG = """Inputs: Len(10);
Vars: A(0), B(0), R(0), Rng(0), Vol(0), Op(0), OI(0);
A = Average(Close, Len);
B = Highest(High, 5);
R = RSI(Close, 14);
Rng = High[1] - Low[1];
Vol = Volume;
Op = Open;
OI = OpenInt;
If Close > A and Close[1] <= A[1] Then Buy ("LE") Next Bar At Market;
If Close < A and Close[1] >= A[1] Then Sell ("LX") Next Bar At Market;
If R > 70 and MarketPosition = 0 Then SellShort ("SE") Next Bar At Market;
If R < 45 Then BuyToCover ("SX") Next Bar At Market;
"""

SHORT = """Inputs: Len(10);
Vars: A(0), B(0), R(0), Rng(0), Vol(0), Op(0), OI(0);
A = Average(C, Len);
B = Highest(h, 5);
R = RSI(C, 14);
Rng = H[1] - l[1];
Vol = V;
Op = o;
OI = I;
If c > A and C[1] <= A[1] Then Buy ("LE") Next Bar At Market;
If C < A and C[1] >= A[1] Then Sell ("LX") Next Bar At Market;
If R > 70 and MarketPosition = 0 Then Sell Short ("SE") Next Bar At Market;
If R < 45 Then Buy To Cover ("SX") Next Bar At Market;
"""


# Same shape restricted to constructs the native kernel supports (no Highest/RSI),
# for the native digest gates.
NATIVE_LONG = """Inputs: Len(10);
Vars: A(0), M(0), Rng(0), Vol(0), Op(0);
A = Average(Close, Len);
M = Close - Close[5];
Rng = High[1] - Low[1];
Vol = Volume;
Op = Open;
If Close > A and Close[1] <= A[1] Then Buy ("LE") Next Bar At Market;
If Close < A and Close[1] >= A[1] Then Sell ("LX") Next Bar At Market;
If M < -2 and MarketPosition = 0 Then SellShort ("SE") Next Bar At Market;
If M > 1 Then BuyToCover ("SX") Next Bar At Market;
"""

NATIVE_SHORT = """Inputs: Len(10);
Vars: A(0), M(0), Rng(0), Vol(0), Op(0);
A = Average(c, Len);
M = C - C[5];
Rng = H[1] - L[1];
Vol = v;
Op = O;
If C > A and C[1] <= A[1] Then Buy ("LE") Next Bar At Market;
If C < A and c[1] >= A[1] Then Sell ("LX") Next Bar At Market;
If M < -2 and MarketPosition = 0 Then Sell Short ("SE") Next Bar At Market;
If M > 1 Then Buy To Cover ("SX") Next Bar At Market;
"""


def _ast(src):
    tokens, positions = tokenise(src)
    return parse(tokens, positions=positions, source=src)


def _strip(node):
    """AST without source-position side channels."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if not k.startswith("_")}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def test_abbreviations_parse_to_long_form():
    assert _strip(_ast(SHORT)) == _strip(_ast(LONG))


def test_abbreviations_codegen_matches_long_form():
    for persist in (False, True):
        assert transpile(SHORT, persist=persist) == transpile(LONG, persist=persist)


def test_two_word_order_verbs():
    for two, one in (("Sell Short", "SellShort"), ("sell short", "SellShort"),
                     ("Buy To Cover", "BuyToCover"), ("buy to cover", "BuyToCover")):
        a = f'If Close > 0 Then {two} ("X") Next Bar At Market;\n'
        b = f'If Close > 0 Then {one} ("X") Next Bar At Market;\n'
        assert _strip(_ast(a)) == _strip(_ast(b)), two


def test_digest_matches_long_form_python():
    py_long = run_oracle(LONG, _bars(), NQ)
    assert py_long["fills"] > 0, py_long["text"]
    assert run_oracle(SHORT, _bars(), NQ)["text"] == py_long["text"]


@pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)
def test_digest_matches_long_form_native():
    long_src, short_src = NATIVE_LONG, NATIVE_SHORT
    py_long = run_oracle(long_src, _bars(), NQ)
    py_short = run_oracle(short_src, _bars(), NQ)
    nat_long = run_oracle(long_src, _bars(), NQ, use_native=True)
    nat_short = run_oracle(short_src, _bars(), NQ, use_native=True)
    assert py_long["fills"] > 0, py_long["text"]
    assert nat_long["native"] is True and nat_short["native"] is True
    assert py_short["text"] == py_long["text"]
    assert nat_short["text"] == nat_long["text"] == py_long["text"]


def test_declared_name_shadows_abbreviation():
    # Documented behaviour: a declared input/variable named like an
    # abbreviation wins over the price series.
    src = """Inputs: C(3);
Vars: L(0), X(0);
L = C + 1;
X = L;
"""
    tree = _strip(_ast(src))
    names = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "ident":
                names.append(n["name"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(tree)
    assert "c" in names and "l" in names
    assert "close" not in names and "low" not in names


@pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)
def test_shadowed_abbreviation_runs_on_both_paths():
    shadow = NATIVE_LONG.replace("Inputs: Len(10);", "Inputs: Len(10), C(0);").replace(
        "Close > A and", "Close + C > A and")
    py = run_oracle(shadow, _bars(), NQ)
    nat = run_oracle(shadow, _bars(), NQ, use_native=True)
    assert nat["native"] is True
    assert py["text"] == nat["text"] == run_oracle(NATIVE_LONG, _bars(), NQ)["text"]


def test_name_declared_later_shadows_abbreviation():
    # A declaration after the first use still shadows the abbreviation.
    tree = _strip(_ast("Value1 = I;\nVars: I(0);\n"))
    text = repr(tree)
    assert "'name': 'i'" in text and "openint" not in text
