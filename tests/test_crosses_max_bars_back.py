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

"""`crosses` under MaxBarsBack (#161). A price series holds the skipped
warm-up bars, while a variable's history starts on the first evaluated bar,
so the two operands differ in length. The cross compares them right-aligned:
the prior relation comes from the previous bar both operands hold. When an
operand is a variable, that is the previous evaluated bar, and the first
evaluated bar never crosses."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import main as oracle_main  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars, run_el  # noqa: E402

NQ = get_config("NQ")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT1 = os.path.join(REPO, "ground_truth", "GT1_functions_indicators.txt")

# Both operand orders, so either side may be the longer history.
CROSS_EL = (
    "Vars: AvgC(0), Up(0), Dn(0);\n"
    "AvgC = Average(Close, 10);\n"
    "Up = 0; Dn = 0;\n"
    "If Close crosses above AvgC Then Up = 1;\n"
    "If AvgC crosses above Close Then Dn = 1;\n"
    "If Up = 1 Then Buy (\"LE\") next bar at market;\n"
    "If Dn = 1 Then SellShort (\"SE\") next bar at market;\n"
)
MBB_VALUES = [0, 1, 10, 50, 200, 100000]


def _expected(bars, mbb):
    """Reference: Close vs the 10-bar average on evaluated bars only, with
    the prior relation taken from the last evaluated bar where they differ."""
    up, dn, rel = [], [], 0
    for i in range(mbb, len(bars)):
        c = bars[i]["close"]
        w = [b["close"] for b in bars[max(0, i - 9):i + 1]]
        avg = sum(w) / 10 if len(w) == 10 else 0.0
        up.append(1 if c > avg and rel < 0 else 0)
        dn.append(1 if avg > c and rel > 0 else 0)
        if c != avg:
            rel = -1 if c < avg else 1
    return up, dn


@pytest.mark.parametrize("mbb", MBB_VALUES)
def test_crosses_runs_and_never_fires_on_first_evaluated_bar(mbb):
    bars = load_bars(SAMPLE)
    out = run_el(CROSS_EL, bars, NQ, trace_columns=["Up", "Dn"], max_bars_back=mbb)
    assert out["n_rows"] == max(0, len(bars) - mbb)
    up = [int(float(r[0])) for r in out["rows"]]
    dn = [int(float(r[1])) for r in out["rows"]]
    if mbb >= 10:
        # Average is warm on every evaluated bar, so the reference is exact.
        assert (up, dn) == _expected(bars, mbb)
    if up:
        assert up[0] == 0 and dn[0] == 0


@pytest.mark.skipif(not accel_available(), reason="accel extension not built")
@pytest.mark.parametrize("mbb", MBB_VALUES)
def test_crosses_native_matches_python(mbb):
    bars = load_bars(SAMPLE)
    py = run_oracle(CROSS_EL, bars, NQ, max_bars_back=mbb)
    nat = run_oracle(CROSS_EL, bars, NQ, max_bars_back=mbb, use_native=True)
    assert nat["native"], nat.get("native_fallback_reason")
    assert nat["text"] == py["text"]
    if mbb < len(bars) - 20:
        assert py["fills"] > 0


@pytest.mark.parametrize("flags", [[], ["--python"]])
def test_gt1_max_bars_back_50_runs(flags, capsys):
    assert oracle_main([GT1, SAMPLE, "--max-bars-back", "50", *flags]) == 0
    assert "sha256:" in capsys.readouterr().out
