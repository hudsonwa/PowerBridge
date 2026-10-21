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

"""MaxBarsBack: the first N bars are history only (no evaluation, no orders,
BarNumber starts at 1 on bar N+1), checked against a MultiCharts capture.
Default 0 keeps every existing digest; a short lookback warns once."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle, main as oracle_main  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars, run_el  # noqa: E402

NQ = get_config("NQ")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
PROGRAMS = {
    "ma_cross": os.path.join(REPO, "examples", "ma_cross.el"),
    "GT2": os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt"),
}
# Default (max_bars_back=0) digests: unchanged by MaxBarsBack support.
DEFAULT_SHA = {
    "ma_cross": "f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da",
    "GT2": "ac9898885cfb87aca896ea2ade1e36ef52c9d689dd179913674262c475d9a5f7",
}
WARNING = "lookback past available history on bar 1"

EVERY_BAR_EL = (
    "Vars: n(0);\n"
    "n = BarNumber;\n"
    "If MarketPosition = 0 Then Buy (\"LE\") next bar at market;\n"
)


def _tape(n=30):
    bars = []
    for i in range(n):
        px = 15000.0 + i
        bars.append({"date": 1240102, "time": 930 + i, "open": px, "high": px + 1,
                     "low": px - 1, "close": px + 0.5, "volume": 100})
    return bars


def _src(name):
    with open(PROGRAMS[name], encoding="utf-8") as f:
        return f.read()


def test_warm_up_bars_place_no_orders_and_barnumber_starts_at_1():
    bars = _tape()
    out = run_el(EVERY_BAR_EL, bars, NQ, trace_columns=["BarNumber", "n"],
                 max_bars_back=10)
    assert out["n_rows"] == len(bars) - 10
    assert [r[0] for r in out["rows"][:3]] == ["1", "2", "3"]
    assert float(out["rows"][0][1]) == 1.0
    # The first order is placed on bar 11 of the tape and fills on bar 12.
    first = out["trades"][0]
    assert (int(first["date"]), int(first["time"])) == (1240102, bars[11]["time"])

    full = run_el(EVERY_BAR_EL, bars, NQ, trace_columns=["BarNumber"])
    assert full["n_rows"] == len(bars)
    assert full["trades"][0]["time"] == bars[1]["time"]


def test_max_bars_back_past_the_tape_evaluates_nothing():
    bars = _tape(5)
    out = run_el(EVERY_BAR_EL, bars, NQ, max_bars_back=50)
    assert out["n_rows"] == 0 and out["trades"] == []
    assert run_oracle(EVERY_BAR_EL, bars, NQ, max_bars_back=50)["fills"] == 0


def test_negative_max_bars_back_is_refused():
    with pytest.raises(ValueError):
        run_el(EVERY_BAR_EL, _tape(), NQ, max_bars_back=-1)


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_default_digests_unchanged(name):
    bars = load_bars(SAMPLE)
    assert run_oracle(_src(name), bars, NQ)["sha256"] == DEFAULT_SHA[name]
    assert run_oracle(_src(name), bars, NQ, max_bars_back=0)["sha256"] == DEFAULT_SHA[name]


@pytest.mark.skipif(not accel_available(), reason="accel extension not built")
@pytest.mark.parametrize("mbb", [0, 50])
@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_native_matches_python(name, mbb):
    bars = load_bars(SAMPLE)
    py = run_oracle(_src(name), bars, NQ, max_bars_back=mbb)
    nat = run_oracle(_src(name), bars, NQ, max_bars_back=mbb, use_native=True)
    assert nat["native"], nat.get("native_fallback_reason")
    assert nat["text"] == py["text"]


def test_gt2_max_bars_back_50_drops_warm_up_trades():
    bars = load_bars(SAMPLE)
    out = run_oracle(_src("GT2"), bars, NQ, max_bars_back=50)
    assert out["sha256"] != DEFAULT_SHA["GT2"]
    first_eval = (int(bars[50]["date"]), int(bars[50]["time"]))
    for rec in out["records"]:
        assert (int(rec["date"]), int(rec["time"])) >= first_eval


def test_older_kernel_falls_back_to_python(monkeypatch):
    import pl_transpiler.runtime.accel as accel_mod

    class OldKernel:
        FILL_LOG_VERSION = accel_mod.FILL_LOG_VERSION

    monkeypatch.setattr(accel_mod, "_import_accel", lambda: OldKernel)
    with pytest.raises(accel_mod.AccelRefused, match="predates max_bars_back"):
        accel_mod.run_native(EVERY_BAR_EL, _tape(), NQ, max_bars_back=5)


def test_short_lookback_warns_once(capsys):
    el = "Vars: x(0);\nx = Close[5];\nIf Close > Close[3] Then Buy next bar at market;\n"
    bars = _tape()
    a = run_el(el, bars, NQ)
    err = capsys.readouterr().err
    assert err.count(WARNING) == 1
    assert "--max-bars-back" in err
    b = run_el(el, bars, NQ, max_bars_back=5)
    assert WARNING not in capsys.readouterr().err
    # The warning never changes values.
    c = run_el(el, bars, NQ)
    capsys.readouterr()
    assert a["rows"] == c["rows"] and a["trades"] == c["trades"]
    assert b["n_rows"] == len(bars) - 5


def test_cli_flag(tmp_path, capsys):
    el = tmp_path / "s.el"
    el.write_text(EVERY_BAR_EL, encoding="utf-8")
    assert oracle_main([str(el), SAMPLE, "--python", "--max-bars-back", "50"]) == 0
    with pytest.raises(SystemExit) as exc:
        oracle_main([str(el), SAMPLE, "--python", "--max-bars-back", "-1"])
    assert exc.value.code == 2
