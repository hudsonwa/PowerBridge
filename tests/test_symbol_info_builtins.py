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
"""test_symbol_info_builtins.py — Symbol, SymbolName, Description, Category,
SymbolCurrencyCode and ExpirationDate (issue #36).

All six read the run's InstrumentConfig, like BigPointValue and PriceScale.

Stdlib only, no captures.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime import pl_runtime                             # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config           # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle                     # noqa: E402
from pl_transpiler.tools.pl_run import run_el                            # noqa: E402

NQ = get_config("NQ")
ES = get_config("ES")

SYMBOL_EL = """Vars: Sym(""), SymNm(""), Desc(""), Cat(-1), Ccy(-1), Exp(-1),
    SymLen(0), Line("");
Sym = Symbol; SymNm = SymbolName(1); Desc = Description;
Cat = Category; Ccy = SymbolCurrencyCode; Exp = ExpirationDate;
Line = Sym + " " + SymNm + " " + Desc;
SymLen = StrLen(Line);
"""


def _bars(n=3):
    return [
        {"date": 1240101 + i, "time": 1000 + i, "open": 100.0 + i,
         "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i, "volume": 1.0}
        for i in range(n)
    ]


def _last_row(config):
    out = run_el(SYMBOL_EL, _bars(), config, trace_columns=None)
    return dict(zip(out["columns"], out["rows"][-1]))


@pytest.mark.parametrize("config", [NQ, ES], ids=["NQ", "ES"])
def test_values_come_from_the_instrument_config(config):
    row = _last_row(config)
    assert row["sym"] == config.symalt
    assert row["symnm"] == config.symnm
    assert row["desc"] == config.descstr
    assert float(row["cat"]) == config.category == 0       # 0 = future
    assert float(row["ccy"]) == config.symccy
    assert float(row["exp"]) == config.expdate


def test_strings_concatenate():
    # The old stubs returned 0/'' — a concatenation must now see the real text.
    row = _last_row(NQ)
    line = f"{NQ.symalt} {NQ.symnm} {NQ.descstr}"
    assert float(row["symlen"]) == len(line)


def test_symbol_info_is_not_unimplemented():
    pl_runtime.pl_unimplemented_reset()
    run_oracle(SYMBOL_EL, _bars(), NQ)
    hit = pl_runtime.pl_unimplemented_report()
    names = {"pl_symbol", "pl_symbolname", "pl_description", "pl_category",
             "pl_symbolcurrencycode", "pl_expirationdate"}
    assert not names & set(hit)


def test_numeric_symbol_info_gates_trades():
    el = """If Category = 0 and ExpirationDate > 0 and MarketPosition = 0
    Then Buy next bar at market;
"""
    assert run_oracle(el, _bars(), NQ)["records"]
