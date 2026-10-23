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

"""max_entries_per_direction: the default (None) keeps every same-direction
entry adding to the position; N >= 1 refuses an entry while N entries are
open in that direction, reversals still fill, and native == Python."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import main as oracle_main
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars, run_el  # noqa: E402

NQ = get_config("NQ")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
LONG_EL = "If Close > Close[1] Then Buy next bar at market;"
REVERSE_EL = (LONG_EL + "\nIf Close < Close[1] Then SellShort next bar at market;")


def _bars():
    return load_bars(SAMPLE)


def _run(el, n=None, **kw):
    # max_bars_back=1 so Close[1] always has history (no warm-up warning).
    return run_oracle(el, _bars(), NQ, max_bars_back=1, max_entries_per_direction=n, **kw)


def _max_long(trades):
    pos = peak = 0
    for t in trades:
        pos += 1 if t["type"] == "EntryLong" else -1
        peak = max(peak, pos)
    return peak


def test_default_is_unlimited():
    default = _run(LONG_EL)
    assert _run(LONG_EL, 0)["sha256"] == default["sha256"]
    assert default["fills"] > 2  # every up close adds a contract


def test_one_refuses_repeat_entries():
    out = _run(LONG_EL, 1)
    assert out["fills"] == 1
    assert _max_long(out["records"]) == 1


def test_two_allows_two_entries():
    out = _run(LONG_EL, 2)
    assert out["fills"] == 2
    assert _max_long(out["records"]) == 2


def test_reversals_still_fill():
    default = _run(REVERSE_EL)
    capped = _run(REVERSE_EL, 1)
    assert 0 < capped["fills"] < default["fills"]
    # One opening entry, then every fill is a reversal: close leg + open leg.
    assert capped["round_turns"] == (capped["fills"] - 1) // 2
    assert capped["fills"] % 2 == 1


def test_run_el_honours_the_cap():
    out = run_el(LONG_EL, _bars(), NQ, max_bars_back=1, max_entries_per_direction=1)
    assert len(out["trades"]) == 1


def test_negative_is_refused():
    with pytest.raises(ValueError, match="max_entries_per_direction"):
        _run(LONG_EL, -1)


def test_cli_rejects_zero(capsys):
    with pytest.raises(SystemExit):
        oracle_main([os.path.join(REPO, "examples", "ma_cross.el"), SAMPLE,
                     "--max-entries-per-direction", "0"])
    assert "must be >= 1" in capsys.readouterr().err


@pytest.mark.skipif(not accel_available(), reason="native kernel not built")
@pytest.mark.parametrize("el", [LONG_EL, REVERSE_EL])
@pytest.mark.parametrize("n", [None, 1, 2])
def test_native_matches_python(el, n):
    py = _run(el, n)
    nat = _run(el, n, use_native=True)
    assert nat["native"] is True
    assert nat["sha256"] == py["sha256"]


def test_older_kernel_falls_back_to_python(monkeypatch):
    import pl_transpiler.runtime.accel as accel_mod

    class OldKernel:
        FILL_LOG_VERSION = accel_mod.FILL_LOG_VERSION
        MAX_BARS_BACK = True

    monkeypatch.setattr(accel_mod, "_import_accel", lambda: OldKernel)
    with pytest.raises(accel_mod.AccelRefused, match="predates max_entries_per_direction"):
        accel_mod.run_native(LONG_EL, _bars(), NQ, max_entries_per_direction=1)
    out = _run(LONG_EL, 1, use_native=True)
    assert out["native"] is False
    assert out["sha256"] == _run(LONG_EL, 1)["sha256"]


PARTIAL_EXIT_EL = (
    "If BarNumber = 2 or BarNumber = 3 or BarNumber = 6 or BarNumber = 7 "
    "Then Buy next bar at market;\n"
    "If BarNumber = 5 Then Sell 1 contract next bar at market;")
THIS_BAR_EL = "If BarNumber = 2 or BarNumber = 3 Then Buy this bar on close;"
NAMED_EL = ('If BarNumber = 2 Then Buy ("A") next bar at market;\n'
            'If BarNumber = 3 Then Buy ("B") next bar at market;')


def _fills(el, n):
    py = _run(el, n)
    if accel_available():
        nat = _run(el, n, use_native=True)
        assert nat["native"] is True
        assert nat["sha256"] == py["sha256"]
    return [(r["type"], r["signal"]) for r in py["records"]]


def test_partial_exit_frees_a_slot():
    # Two entries fill, Sell 1 frees one slot, the next entry fills and
    # the one after it is refused again.
    assert _fills(PARTIAL_EXIT_EL, 2) == [
        ("EntryLong", ""), ("EntryLong", ""), ("ExitLong", ""), ("EntryLong", "")]
    assert len(_fills(PARTIAL_EXIT_EL, None)) == 5


def test_this_bar_on_close_repeat_is_refused():
    assert _fills(THIS_BAR_EL, 1) == [("EntryLong", "")]
    assert len(_fills(THIS_BAR_EL, None)) == 2


def test_cap_counts_entries_across_names():
    assert _fills(NAMED_EL, 1) == [("EntryLong", "A")]
    assert _fills(NAMED_EL, None) == [("EntryLong", "A"), ("EntryLong", "B")]
