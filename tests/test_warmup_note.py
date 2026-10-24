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

"""The CLI warm-up note: pl_run and pl_oracle say once on stderr that fills
may differ from MultiCharts during warm-up when a strategy reads past bars
and --max-bars-back is 0. stdout and the library API stay silent."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.lexer import PLSyntaxError  # noqa: E402
from pl_transpiler.lookback import uses_lookback  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402

MA_CROSS = os.path.join(REPO, "examples", "ma_cross.el")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
NOTE = "fills may differ from MultiCharts during warm-up"
NO_LOOKBACK = 'If MarketPosition = 0 Then Buy ("LE") next bar at market;\n'


@pytest.mark.parametrize("src,expected", [
    ("Value1 = Average(Close, 5);", True),
    ("Value1 = Highest(High, 3);", True),
    ("Value1 = Close[1];", True),
    ("Vars: n(3); Value1 = Close[n];", True),
    ("Value1 = Close[0] + Close;", False),
    (NO_LOOKBACK, False),
    ("Value1 = TrueRange;", True),
    ("Value1 = AverageFC(Close, 5);", True),
    ("Value1 = LinearRegValueFC(Close, 5, 0);", True),
    ("Value1 = Median(Close, 5);", True),
    ("Value1 = AvgTrueRange(14);", True),
    ("Value1 = Close of 1 bar ago;", True),
    ("Value1 = Close of 2 bars ago;", True),
    ("Value1 = MyIndexingFunc(Close);", True),
    ("Value1 = Average(Close, 1);", False),
    ("Value1 = Momentum(Close, 1);", True),
    ("Value1 = AbsValue(Close - Open);", False),
])
def test_uses_lookback(src, expected):
    assert uses_lookback(src) is expected


def test_of_zero_bars_ago_is_a_syntax_error():
    """`of N bars ago` is not parsed; with N = 0 the token scan does not
    short-circuit, so the parser reports the missing ';' after `Close`."""
    with pytest.raises(PLSyntaxError, match="missing ';'"):
        uses_lookback("Value1 = Close of 0 bars ago;")


@pytest.mark.parametrize("tool,extra", [
    (pl_oracle, ["--python"]),
    (pl_run, ["--columns", "MarketPosition"]),
])
def test_note_printed_once_and_stdout_unchanged(tool, extra, capsys):
    assert tool.main([MA_CROSS, SAMPLE] + extra) == 0
    noted = capsys.readouterr()
    assert noted.err.count(NOTE) == 1
    assert NOTE not in noted.out

    assert tool.main([MA_CROSS, SAMPLE, "-q"] + extra) == 0
    quiet = capsys.readouterr()
    assert NOTE not in quiet.err
    assert quiet.out == noted.out

    assert tool.main([MA_CROSS, SAMPLE, "--max-bars-back", "1"] + extra) == 0
    assert NOTE not in capsys.readouterr().err


def test_one_warning_when_short_history_also_fires(tmp_path, capsys):
    el = tmp_path / "dip.el"
    el.write_text("If Close < Close[3] Then Buy next bar at market;\n")
    assert pl_oracle.main([str(el), SAMPLE, "--python"]) == 0
    err = capsys.readouterr().err
    assert "lookback past available history" in err
    assert NOTE not in err


def test_no_note_without_lookback(tmp_path, capsys):
    el = tmp_path / "flat.el"
    el.write_text(NO_LOOKBACK)
    assert pl_oracle.main([str(el), SAMPLE, "--python"]) == 0
    assert NOTE not in capsys.readouterr().err


def test_library_api_is_silent(capsys):
    src = open(MA_CROSS, encoding="utf-8").read()
    bars = pl_run.load_bars(SAMPLE)
    pl_oracle.run_oracle(src, bars, get_config("NQ"), use_native=False)
    pl_run.run_el(src, bars, get_config("NQ"), trace_columns=["MarketPosition"])
    assert NOTE not in capsys.readouterr().err
