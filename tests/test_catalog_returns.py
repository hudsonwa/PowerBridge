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

"""test_catalog_returns.py — the ``returns`` field of pl_signatures.jsonl, observed.

Each probe below runs one keyword through PowerBridge (``run_el`` on the synthetic
sample bars) and classifies the value it produces: ``string`` if ``expr + ""``
runs, ``bool`` if a variable assigned ``expr`` traces as True/False, ``numeric`` if
``expr - 0`` runs. The observed type must equal ``catalog.returns()``. The probe
statements are EL string literals, so tools/gen_catalog_fields.py counts them as
``verified:test`` evidence for these keywords.

The file also pins the catalog's gap counts (``unknown`` returns, verified builtins
without a signature record), so a regression or an unreviewed change shows up."""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import catalog, get_config, load_bars_csv, run_el  # noqa: E402

# Arrays the probes pass to array builtins.
_PRELUDE = 'arrays: pa[3](0), pb[3](0), pf[3](false), ps[3]("a"); '

# One EL statement per keyword: "pz = <expression>;".
PROBES = [
    "pz = Black;", "pz = Blue;", "pz = Cyan;", "pz = DarkBlue;", "pz = DarkCyan;",
    "pz = DarkGray;", "pz = DarkGreen;", "pz = DarkMagenta;", "pz = DarkRed;",
    "pz = DarkYellow;", "pz = Green;", "pz = LightGray;", "pz = Magenta;", "pz = Red;",
    "pz = White;", "pz = Yellow;", "pz = OpenEntriesCount;",
    "pz = AlertEnabled;", "pz = BarsSinceExit;", "pz = BarStatus;", "pz = BarType;",
    "pz = BarType_ex;", "pz = BaseDataNumber;", "pz = BigPointValue;", "pz = Category;",
    "pz = CurrentBar;", "pz = CurrentDataNumber;", "pz = CurrentEntries;",
    "pz = CurrentShares;", "pz = DailyClose;", "pz = DailyHigh;", "pz = DailyLow;",
    "pz = DailyOpen;", "pz = DateTime;", "pz = Description;", "pz = EntryDate;",
    "pz = EntryDateTime;", "pz = EntryTime;", "pz = ExecOffset;", "pz = ExpirationDate;",
    "pz = GrossLoss;", "pz = GrossProfit;", "pz = IntervalType;", "pz = IntervalType_ex;",
    "pz = LargestLosTrade;", "pz = LargestWinTrade;", "pz = MaxBarsBack;",
    "pz = MaxBarsForward;", "pz = MaxConsecLosers;", "pz = MaxConsecWinners;",
    "pz = MaxContractsHeld;", "pz = MaxIDDrawDown;", "pz = MaxSharesHeld;",
    "pz = NumEvenTrades;", "pz = NumLosTrades;", "pz = NumWinTrades;",
    "pz = PercentProfit;", "pz = PointValue;", "pz = PriceScale;", "pz = Sess1EndTime;",
    "pz = Sess1FirstBarTime;", "pz = Sess1StartTime;", "pz = Sess2EndTime;",
    "pz = Sess2FirstBarTime;", "pz = Sess2StartTime;", "pz = SessionCount;",
    "pz = SessionCountMS;", "pz = SessionEndDay;", "pz = SessionEndDayMS;",
    "pz = SessionLastBar;", "pz = Symbol;", "pz = SymbolName;", "pz = TickID;",
    "pz = Time_s;", "pz = TotalBarsEvenTrades;", "pz = TotalBarsLosTrades;",
    "pz = TotalBarsWinTrades;", "pz = TradeDate;", "pz = TradeTime;", "pz = TradeVolume;",
    "pz = Array_Compare(pa, 0, pb, 0, 3);", "pz = Array_Contains(pa, 0);",
    "pz = Array_Copy(pa, 0, pb, 0, 3);", "pz = Array_GetBooleanValue(pf, 1);",
    "pz = Array_GetFloatValue(pa, 1);", "pz = Array_GetIntegerValue(pa, 1);",
    "pz = Array_GetMaxIndex(pa);", "pz = Array_GetStringValue(ps, 1);",
    "pz = Array_GetType(pa);", "pz = Array_IndexOf(pa, 0);",
    "pz = Array_SetMaxIndex(pa, 5);", "pz = Array_SetValRange(pa, 0, 2, 1);",
    "pz = Array_Sort(pa, 0, 2, true);", "pz = Array_Sum(pa, 0, 2);",
    "pz = Fill_Array(pa, 1);", "pz = Text_New(Date, Time, Close, \"t\");",
    "pz = Text_New_s(Date, Time_s, Close, \"t\");",
]

# Verified builtins with no signature record, and why.
UNSIGNED = {
    "abort": "a statement, not an expression: the parser rejects it as a value",
    "commandline": "not implemented in the runtime (a loud stub), so no type is observable",
    "entryname": "not implemented: transpiling it raises UnimplementedKeywordError",
}

# Pinned gap counts; update them only with a reviewed catalog change.
UNKNOWN_RETURNS = 171        # 54 syntax words + 117 builtins
UNKNOWN_BUILTIN_RETURNS = 117

_BARS = None


def _run(src):
    global _BARS
    if _BARS is None:
        _BARS = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))[:30]
    try:
        return run_el(_PRELUDE + src, _BARS, get_config("NQ"), trace_columns=["pz"])["rows"][-1][0]
    except Exception:
        return None


def _observe(expr):
    if _run(f'vars: pz(""); pz = {expr} + "";') is not None:
        return "string"
    if _run(f"vars: pz(false); pz = {expr};") in ("True", "False"):
        return "bool"
    if _run(f"vars: pz(0); pz = {expr} - 0;") is not None:
        return "numeric"
    return "unknown"


def _parse(stmt):
    m = re.fullmatch(r"pz = (([A-Za-z_]\w*).*);", stmt)
    return m.group(2), m.group(1)


@pytest.mark.parametrize("stmt", PROBES)
def test_observed_return_matches_catalog(stmt):
    name, expr = _parse(stmt)
    observed = _observe(expr)
    assert observed != "unknown", f"{name}: PowerBridge produced no value for {expr!r}"
    assert catalog.returns(name) == observed, (name, catalog.returns(name), observed)


def test_probe_classifier_sanity():
    assert _observe("Close") == "numeric"
    assert _observe("Close > Open") == "bool"
    assert _observe('"x"') == "string"


def test_unknown_return_counts_pinned():
    recs = [catalog.get(n) for n in catalog.search("")]
    unknown = [r for r in recs if r["returns"] == "unknown"]
    assert len(unknown) == UNKNOWN_RETURNS
    assert sum(r["kind"] == "builtin" for r in unknown) == UNKNOWN_BUILTIN_RETURNS
    # An unknown return is never presented as backed by a PowerBridge test.
    assert not [r["keyword"] for r in unknown if r["verification"] == "verified:test"
                and r["keyword"].lower() in {_parse(p)[0].lower() for p in PROBES}]


def test_unsigned_verified_builtins_documented():
    sigs = {n.lower() for n in catalog.search("")}
    unsigned = {t for t in catalog._load_verified_builtins() if t not in sigs}
    assert unsigned == set(UNSIGNED)
    cov = catalog.coverage()
    assert cov["verified_builtins"] - cov["verified_with_signature"] == len(UNSIGNED)
