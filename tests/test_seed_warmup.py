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

"""
test_seed_warmup.py — issue #12: opt-in run window with seeded warmup.

Default remains the full tape. A window is opt-in via from_date / from_bar.

* Tape slice (seed_warmup=False) drops the prefix. Strategies that use Once,
  Close[n], or a bar-1 cumulative then disagree with the full tape — that is a
  failed check, not a speed win.
* Seeded warmup (seed_warmup=True) runs the prefix with fills suppressed so
  state / history / Once stay primed. The last warmup bar may queue a
  "next bar" order that fills on the first trading bar.

pl_oracle --from-date stays a tape slice (#11 FAST LANE). --seed-warmup opts
into the seeded path. pl_run --from-date defaults to seeded warmup; --slice
drops the prefix.

Stdlib only, no captures.
"""
import contextlib
import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.engine import FillEngine, run_bar_loop  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle, main as oracle_main  # noqa: E402
from pl_transpiler.tools.pl_run import (  # noqa: E402
    resolve_trade_from, run_el, main as pl_run_main,
)

NQ = get_config("NQ")

ONCE_EL = (
    "Vars: FirstClose(0);\n"
    "Once FirstClose = Close;\n"
    "If Date >= 1240104 and Close > FirstClose Then Buy (\"LE\") next bar at market;\n"
)

COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 5 Then Buy (\"LE\") next bar at market;\n"
)

LOOKBACK_EL = (
    "If Date >= 1240104 and Close > Close[1] Then Buy (\"LE\") next bar at market;\n"
)


def _dated_bars(n, date0=1240102, close0=10.0):
    return [
        {"date": date0 + i, "time": 930, "open": close0 + i,
         "high": close0 + i, "low": close0 + i, "close": close0 + i,
         "volume": 1.0}
        for i in range(n)
    ]


def _write_csv(path, bars):
    with open(path, "w", encoding="utf-8") as f:
        f.write("date,time,open,high,low,close,volume\n")
        for b in bars:
            f.write(f"{b['date']},{b['time']},{b['open']},{b['high']},"
                    f"{b['low']},{b['close']},{b['volume']}\n")


def test_resolve_trade_from():
    bars = _dated_bars(5)
    assert resolve_trade_from(bars) == 0
    assert resolve_trade_from(bars, from_date=1240104) == 2
    assert resolve_trade_from(bars, from_bar=3) == 3
    assert resolve_trade_from(bars, from_date=1240199) == 5
    assert resolve_trade_from(bars, from_bar=99) == 5
    try:
        resolve_trade_from(bars, from_date=1, from_bar=1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        resolve_trade_from(bars, from_bar=-1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("  [ok] resolve_trade_from date/bar/exclusive/clamp")


def test_trade_from_zero_is_identity():
    """trade_from=0 must not change fills or emit count vs the default loop."""
    bars = _dated_bars(8)
    full = run_oracle(COUNT_EL, bars, NQ)
    same = run_oracle(COUNT_EL, bars, NQ, from_bar=0, seed_warmup=True)
    assert same["text"] == full["text"]
    assert same["n_warmup"] == 0
    assert same["n_bars"] == full["n_bars"]
    print("  [ok] trade_from=0 digest identical to full tape")


def test_once_slice_disagrees_seed_matches():
    """Once binds FirstClose on bar 1 of whatever tape it sees.

    Slice from 1240104 rebinds FirstClose to that bar's close and misses the
    fill. Seeded warmup fires Once on the real first bar, so the digest matches
    the full tape.
    """
    bars = _dated_bars(5)  # closes 10..14, dates 1240102..1240106
    full = run_oracle(ONCE_EL, bars, NQ)
    sliced = run_oracle(ONCE_EL, bars, NQ, from_date=1240104)
    seeded = run_oracle(ONCE_EL, bars, NQ, from_date=1240104, seed_warmup=True)
    assert sliced["text"] != full["text"], (
        "tape slice of an Once strategy must disagree — otherwise the test "
        "does not prove seeded warmup is doing work")
    assert seeded["text"] == full["text"], (seeded["records"], full["records"])
    assert seeded["n_warmup"] == 2
    assert seeded["n_bars"] == 5
    assert sliced["n_bars"] == 3
    print("  [ok] Once: slice disagrees, seed_warmup matches full")


def test_count_slice_disagrees_seed_matches():
    """Count is a bar-1 cumulative. Slicing resets it; seeding does not."""
    bars = _dated_bars(8)
    full = run_oracle(COUNT_EL, bars, NQ)
    sliced = run_oracle(COUNT_EL, bars, NQ, from_bar=3)
    seeded = run_oracle(COUNT_EL, bars, NQ, from_bar=3, seed_warmup=True)
    assert full["fills"] == 1, full["records"]
    assert sliced["text"] != full["text"]
    assert seeded["text"] == full["text"], (seeded["records"], full["records"])
    assert seeded["n_warmup"] == 3
    print("  [ok] Count=5: slice disagrees, seed_warmup matches full")


def test_lookback_slice_disagrees_seed_matches():
    """Close[1] on a sliced tape is the new first-bar fallback (current Open),
    not the previous tape bar. Seeded warmup keeps the real previous close.

    Last warmup bar may still queue Buy next bar, which fills on the first
    trading bar — matching full-tape timing.
    """
    bars = _dated_bars(5)
    full = run_oracle(LOOKBACK_EL, bars, NQ)
    sliced = run_oracle(LOOKBACK_EL, bars, NQ, from_date=1240104)
    seeded = run_oracle(LOOKBACK_EL, bars, NQ, from_date=1240104, seed_warmup=True)
    assert sliced["text"] != full["text"], (sliced["records"], full["records"])
    assert seeded["text"] == full["text"], (seeded["records"], full["records"])
    print("  [ok] Close[1]: slice disagrees, seed_warmup matches full")


def test_late_window_is_failed_ticket():
    """A window that starts after the signal bar disagrees — not a speed win."""
    bars = _dated_bars(5)
    full = run_oracle(ONCE_EL, bars, NQ)
    late = run_oracle(ONCE_EL, bars, NQ, from_date=1240106, seed_warmup=True)
    assert late["text"] != full["text"]
    print("  [ok] late window disagrees (failed check, not a speed win)")


def test_run_el_default_unchanged_and_seed_window():
    bars = _dated_bars(8)
    full = run_el(COUNT_EL, bars, NQ, retain_rows=False)
    assert full["n_warmup"] == 0
    assert full["n_rows"] == 8
    seeded = run_el(COUNT_EL, bars, NQ, from_bar=3, retain_rows=False)
    assert seeded["n_warmup"] == 3
    assert seeded["n_rows"] == 5  # emit starts at the window
    assert seeded["trades"] == full["trades"]
    sliced = run_el(COUNT_EL, bars, NQ, from_bar=3, seed_warmup=False,
                    retain_rows=False)
    assert sliced["n_warmup"] == 0
    assert sliced["n_rows"] == 5
    assert sliced["trades"] != full["trades"]
    print("  [ok] run_el default full tape; seed matches; slice disagrees")


def test_engine_trade_from_rejects_bad_index():
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False

    def strategy(*_a, **_k):
        return {"orders": [], "risk": {}}

    try:
        run_bar_loop(
            strategy, n_bars=2,
            get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
            dates=[1240102, 1240103], times=[930, 931],
            config=NQ, fill_engine=fe, emit_row=lambda *a: None,
            strict=True, trade_from=3,
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("  [ok] trade_from > n_bars raises")


def test_oracle_cli_seed_warmup():
    bars = _dated_bars(8)
    with tempfile.TemporaryDirectory() as td:
        el_path = os.path.join(td, "s.el")
        bars_path = os.path.join(td, "bars.csv")
        with open(el_path, "w", encoding="utf-8") as f:
            f.write(COUNT_EL)
        _write_csv(bars_path, bars)
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = oracle_main([el_path, bars_path, "--from-bar", "3",
                              "--seed-warmup"])
        assert rc == 0
        lib = run_oracle(COUNT_EL, bars, NQ, from_bar=3, seed_warmup=True)
        assert buf.getvalue() == lib["text"]
        assert "3 warmup" in err.getvalue()
        sliced_buf = io.StringIO()
        with contextlib.redirect_stdout(sliced_buf), contextlib.redirect_stderr(io.StringIO()):
            rc2 = oracle_main([el_path, bars_path, "--from-bar", "3"])
        assert rc2 == 0
        assert sliced_buf.getvalue() != buf.getvalue()
    print("  [ok] pl_oracle --seed-warmup vs default slice")


def test_pl_run_cli_from_bar_seeds():
    bars = _dated_bars(8)
    with tempfile.TemporaryDirectory() as td:
        el_path = os.path.join(td, "s.el")
        bars_path = os.path.join(td, "bars.csv")
        out_path = os.path.join(td, "out.csv")
        with open(el_path, "w", encoding="utf-8") as f:
            f.write(COUNT_EL)
        _write_csv(bars_path, bars)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pl_run_main([el_path, bars_path, "--from-bar", "3",
                              "--columns", "currentbar", "-o", out_path])
        assert rc == 0
        assert "3 warmup" in err.getvalue()
        rows = open(out_path, encoding="utf-8").read().strip().splitlines()
        # header + 5 emitted bars (window start through end)
        assert len(rows) == 6, rows
        err2 = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err2):
            rc2 = pl_run_main([el_path, bars_path, "--from-bar", "3", "--slice",
                               "--columns", "currentbar", "-o", out_path])
        assert rc2 == 0
        assert "warmup" not in err2.getvalue()
    print("  [ok] pl_run --from-bar seeds; --slice drops prefix")


def main():
    tests = [
        test_resolve_trade_from,
        test_trade_from_zero_is_identity,
        test_once_slice_disagrees_seed_matches,
        test_count_slice_disagrees_seed_matches,
        test_lookback_slice_disagrees_seed_matches,
        test_late_window_is_failed_ticket,
        test_run_el_default_unchanged_and_seed_window,
        test_engine_trade_from_rejects_bad_index,
        test_oracle_cli_seed_warmup,
        test_pl_run_cli_from_bar_seeds,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL SEED-WARMUP TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
