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
test_oracle.py — exact trade-list digest.

The oracle runs persist=True / min-emit and prints a canonical digest.
Two runs on unchanged code must be byte-identical. persist is required:
the clean mirror dialect resets Variables each bar and would miss fills.

Stdlib only. Capture-gated GT2 check skips when captures are absent.
"""
import contextlib
import hashlib
import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.bar_tape import ohlcv_columns  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import (  # noqa: E402
    canonical_fill_line,
    fill_trade_types,
    fills_sha256,
    format_digest,
    run_oracle,
    window_bars,
    write_fills_csv,
)
from pl_transpiler.tools.pl_oracle import (
    main as oracle_main,
)
from pl_transpiler.tools.pl_run import load_bars, load_bars_csv, run_el  # noqa: E402
from tests import capture_gate  # noqa: E402
from tests.reference_pins import (  # noqa: E402
    AUDIT_REF_3_DIGEST,
    AUDIT_REF_3_FILLS,
    AUDIT_REF_3_ROUND_TURNS,
    AUDIT_REF_3_SHA256,
)
from tests.skip_messages import AUDIT_TAPE_SKIP  # noqa: E402

NQ = get_config("NQ")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 3 Then Buy (\"LE\") next bar at market;\n"
)

DATE_EL = (
    "Vars: Armed(False);\n"
    "If Date >= 1240104 and not Armed Then Begin\n"
    "    Buy (\"LE\") next bar at market;\n"
    "    Armed = True;\n"
    "End;\n"
)

DATA2_SOURCE = (
    "Variables: D2C(0);\n"
    "D2C = Close of Data2;\n"
    "If D2C > Close Then Buy (\"D2B\") 1 Contracts Next Bar At D2C Limit;\n"
)

# Reference audit window the silent-ignore bug was measured on. The audit tape
# is not shipped; tests that need it are gated on PB_AUDIT_EL / PB_AUDIT_BARS /
# PB_AUDIT_D2, and the numbers are the public digest pin. They are v1 digests
# (pinned before v2), so those tests run with digest_version=1.
AUDIT_REF_3_WINDOW = slice(1515000, 1616000)
AUDIT_REF_3_WINDOW_FILLS = 928
AUDIT_REF_3_WINDOW_SHA256 = (
    "d8ad8dbbf1c7950f03e2e6df0f4bb81a93dc192a3937bcb6e0e3b9e432041d3a"
)
# The silent-ignore answer (data2_bars dropped, Close of Data1 used instead).
# A regression that returns this has resurrected the bug.
AUDIT_REF_3_WINDOW_SILENT_FILLS = 948
AUDIT_REF_3_WINDOW_SILENT_SHA256 = (
    "608c16f090fa6b3146071f3d2d5bf61dfa10a8b2ac9deb7a869e864bd7b81778"
)

# Frozen GT2 digest on the shipped sample tape. A fill-engine change that
# moves a trade on this 140-bar file MUST fail this test. v2 logs the three
# risk exits (trailing stop, profit target, trailing stop) v1 left out.
GT2_SAMPLE_DIGEST = (
    "powerbridge-trade-digest v2\n"
    "fills: 7\n"
    "round_turns: 3\n"
    "sha256: ac9898885cfb87aca896ea2ade1e36ef52c9d689dd179913674262c475d9a5f7\n"
)
# The same run as a v1 digest (`--digest-v1`), byte-identical to the pre-v2 pin.
GT2_SAMPLE_DIGEST_V1 = (
    "powerbridge-trade-digest v1\n"
    "fills: 4\n"
    "round_turns: 2\n"
    "sha256: 2ce936cec6a9ccf3883ca5896a8537ab6e2e994ca3346448b9ae9bc41745f049\n"
)

# v2 hashes a "digest_version: 2" line ahead of the fills; v1 hashed nothing.
EMPTY_SHA256 = hashlib.sha256(b"digest_version: 2\n").hexdigest()
EMPTY_SHA256_V1 = hashlib.sha256(b"").hexdigest()


def _bars(n, date0=1240102, open0=1.0):
    return [
        {"date": date0, "time": 930 + i, "open": open0, "high": open0,
         "low": open0, "close": open0, "volume": 1.0}
        for i in range(n)
    ]


def _dated_bars(n, date0=1240102):
    return [
        {"date": date0 + i, "time": 930, "open": 10.0 + i, "high": 10.0 + i,
         "low": 10.0 + i, "close": 10.0 + i, "volume": 1.0}
        for i in range(n)
    ]


def test_reference_audit_citation_constants():
    """The reference audit numbers later changes must cite. The tape is not shipped; this
    only checks the constant matches the formatter so a typo cannot drift."""
    assert AUDIT_REF_3_FILLS == 7364
    assert AUDIT_REF_3_ROUND_TURNS == 3682
    assert len(AUDIT_REF_3_SHA256) == 64
    assert AUDIT_REF_3_DIGEST == format_digest(
        fills=AUDIT_REF_3_FILLS, round_turns=AUDIT_REF_3_ROUND_TURNS, sha256=AUDIT_REF_3_SHA256,
        version=1)
    print("  [ok] reference audit citation constants 7364/3682 + sha256 formatter")


def test_empty_tape_digest():
    d = run_oracle(COUNT_EL, [], NQ, max_bars_back=0)
    assert d["fills"] == 0
    assert d["round_turns"] == 0
    assert d["n_bars"] == 0
    assert d["sha256"] == EMPTY_SHA256
    assert d["text"] == format_digest(
        fills=0, round_turns=0, sha256=EMPTY_SHA256)
    print("  [ok] empty tape: 0 fills, SHA-256 of the version line alone")


def test_count_strategy_needs_persist():
    """Clean dialect would reset Count each bar; oracle uses persist=True."""
    d = run_oracle(COUNT_EL, _bars(5), NQ, max_bars_back=0)
    assert d["fills"] == 1, d["records"]
    assert d["round_turns"] == 0  # still open (fills//2)
    assert d["records"][0]["signal"] == "LE"
    assert d["records"][0]["type"] == "EntryLong"
    a = run_oracle(COUNT_EL, _bars(5), NQ, max_bars_back=0)
    assert a["text"] == d["text"]
    print("  [ok] Count=3 Buy: 1 fill, 0 round-turns, two runs identical")


def test_hasher_matches_payload():
    fills = [{
        "type": "EntryLong", "signal": "LE", "date": 1240102,
        "time": 933, "price": 1.0, "contracts": 1,
    }]
    payload = canonical_fill_line(fills[0]) + "\n"
    v2 = "digest_version: 2\n" + payload
    assert fills_sha256(fills) == hashlib.sha256(v2.encode("utf-8")).hexdigest()
    assert fills_sha256(fills, 1) == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert fills_sha256([]) == EMPTY_SHA256
    assert fills_sha256([], 1) == EMPTY_SHA256_V1
    print("  [ok] sha256 is SHA-256 of the version line + canonical fill lines")


def _side_fills(sides):
    """Synthetic one-contract records from a 'B'/'S' side string."""
    return [{"type": "EntryLong" if c == "B" else "ExitLong", "signal": "X",
             "date": 1240102, "time": 930 + i, "price": 1.0, "contracts": 1}
            for i, c in enumerate(sides)]


def test_trade_type_short_from_flat():
    # SellShort from flat hashes as sell side but is an entry-short.
    assert fill_trade_types(_side_fills("S")) == ["EntryShort"]
    assert fill_trade_types(_side_fills("SS")) == ["EntryShort", "EntryShort"]
    print("  [ok] sell from flat -> EntryShort")


def test_trade_type_buy_to_cover():
    # BuyToCover hashes as buy side but exits the short.
    assert fill_trade_types(_side_fills("SB")) == ["EntryShort", "ExitShort"]
    assert fill_trade_types(_side_fills("BS")) == ["EntryLong", "ExitLong"]
    print("  [ok] buy while short -> ExitShort")


def test_trade_type_reversal_splits():
    # Long 1 -> SellShort 1 is two sell-side records: exit the long, enter short.
    assert fill_trade_types(_side_fills("BSSBB")) == [
        "EntryLong", "ExitLong", "EntryShort", "ExitShort", "EntryLong"]
    print("  [ok] reversal -> exit then entry")


def test_fills_csv_keeps_digest_and_matches_native():
    el = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLE)
    py = run_oracle(el, bars, NQ, use_native=False, max_bars_back=0)
    assert py["text"] == GT2_SAMPLE_DIGEST
    labels = fill_trade_types(py["records"])
    assert labels == ["EntryShort", "ExitShort", "EntryLong", "ExitLong",
                      "EntryShort", "ExitShort", "EntryLong"], labels
    if accel_available():
        nat = run_oracle(el, bars, NQ, use_native=True, max_bars_back=0)
        assert nat["text"] == GT2_SAMPLE_DIGEST
        assert fill_trade_types(nat["records"]) == labels
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fills.csv")
        write_fills_csv(path, py["records"])
        rows = open(path, encoding="utf-8").read().splitlines()
    assert rows[0] == ("order_no,trade_type,side,signal,date,time,price,"
                       "contracts,position_after")
    assert rows[1].startswith("1,EntryShort,sell,SE,1240102,1001,"), rows[1]
    assert rows[1].endswith(",1,-1"), rows[1]
    assert len(rows) == len(py["records"]) + 1
    print("  [ok] GT2 labels shorts correctly; native == python")


def test_gt2_sample_digest_frozen():
    el = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLE)
    d = run_oracle(el, bars, NQ, max_bars_back=0)
    assert d["text"] == GT2_SAMPLE_DIGEST, d["text"]
    # persist min-emit must match the trace-mode run_el trade list.
    traced = run_el(el, bars, NQ, retain_rows=False, max_bars_back=0)
    assert traced["trades"] == d["records"]
    print("  [ok] GT2 sample digest frozen; persist trades == run_el trades")


def test_window_identity_and_disagreement():
    bars = _dated_bars(5)  # 1240102 .. 1240106
    full = run_oracle(DATE_EL, bars, NQ, max_bars_back=0)
    same = run_oracle(DATE_EL, bars, NQ, from_date=1240104, max_bars_back=0)
    different = run_oracle(DATE_EL, bars, NQ, from_date=1240105, max_bars_back=0)
    assert same["text"] == full["text"], (same["records"], full["records"])
    assert same["n_bars"] == 3
    assert different["text"] != full["text"]
    sliced, extra = window_bars(bars, from_date=1240104, extra=list(range(5)))
    assert [b["date"] for b in sliced] == [1240104, 1240105, 1240106]
    assert extra == [2, 3, 4]
    try:
        window_bars(bars, extra=[0])
        raise AssertionError("expected ValueError on extra length mismatch")
    except ValueError:
        pass
    print("  [ok] window from signal date matches full; late window disagrees")


def test_cli_digest_and_compare():
    bars = _bars(5)
    with tempfile.TemporaryDirectory() as td:
        el_path = os.path.join(td, "s.el")
        bars_path = os.path.join(td, "bars.csv")
        out_path = os.path.join(td, "digest.txt")
        dump_path = os.path.join(td, "fills.txt")
        with open(el_path, "w", encoding="utf-8") as f:
            f.write(COUNT_EL)
        with open(bars_path, "w", encoding="utf-8") as f:
            f.write("date,time,open,high,low,close,volume\n")
            for b in bars:
                f.write(f"{b['date']},{b['time']},{b['open']},{b['high']},"
                        f"{b['low']},{b['close']},{b['volume']}\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = oracle_main([el_path, bars_path, "-o", out_path, "--dump", dump_path])
        assert rc == 0
        text = open(out_path, encoding="utf-8").read()
        lib = run_oracle(COUNT_EL, bars, NQ, max_bars_back=0)
        assert text == lib["text"]
        assert buf.getvalue() == text
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc_ok = oracle_main([el_path, bars_path, "--compare", out_path])
        assert rc_ok == 0
        bad = os.path.join(td, "bad.txt")
        with open(bad, "w", encoding="utf-8") as f:
            f.write(format_digest(fills=99, round_turns=0, sha256="0" * 64))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc_bad = oracle_main([el_path, bars_path, "--compare", bad])
        assert rc_bad == 1
        dump = open(dump_path, encoding="utf-8").read()
        assert dump == "digest_version: 2\n" + canonical_fill_line(lib["records"][0]) + "\n"
    print("  [ok] CLI writes digest, --compare 0/1, --dump payload")


def test_cli_requires_bars():
    rc = 0
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            oracle_main(["missing.el"])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    print("  [ok] CLI without bars_csv exits 2")


def test_data2_closes_from_bars_aligns_on_date_time():
    """Join is (date, time) last-wins, not positional close-column copy."""
    from pl_transpiler.tools.pl_oracle import data2_closes_from_bars
    bars = [
        {"date": 1240102, "time": 930, "open": 1, "high": 1, "low": 1,
         "close": 10.0, "volume": 1},
        {"date": 1240102, "time": 931, "open": 1, "high": 1, "low": 1,
         "close": 20.0, "volume": 1},
        {"date": 1240102, "time": 932, "open": 1, "high": 1, "low": 1,
         "close": 30.0, "volume": 1},
    ]
    # Extra leading bar, shuffled order, a duplicate timestamp (last-wins),
    # and a Data2-only timestamp that must not leak into Data1.
    data2 = [
        {"date": 1240102, "time": 900, "open": 0, "high": 0, "low": 0,
         "close": 999.0, "volume": 1},
        {"date": 1240102, "time": 932, "open": 0, "high": 0, "low": 0,
         "close": 300.0, "volume": 1},
        {"date": 1240102, "time": 930, "open": 0, "high": 0, "low": 0,
         "close": 100.0, "volume": 1},
        {"date": 1240102, "time": 930, "open": 0, "high": 0, "low": 0,
         "close": 101.0, "volume": 1},  # last-wins for 930
    ]
    got = data2_closes_from_bars(bars, data2)
    # 931 has no Data2 match -> Data1 close (20.0)
    assert got == [101.0, 20.0, 300.0]
    print("  [ok] data2_closes_from_bars: (date, time) last-wins + Data1 fallback")


def test_python_path_consumes_data2_bars():
    """data2_bars must drive the Python path. Silent drop is the forbidden outcome.

    On today's (pre-fix) code this returns the no-Data2 digest: a plausible
    fill count with empty stderr. After the fix, data2_bars and data2_closes
    produce the same digest, and that digest is not the Data1 fallback.
    """
    bars = load_bars_csv(SAMPLE)
    data2_tape = [dict(b, close=b["close"] + 3.0) for b in bars]
    data2_closes = [b["close"] for b in data2_tape]

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        via_bars = run_oracle(DATA2_SOURCE, bars, NQ, data2_bars=data2_tape, max_bars_back=0)
    via_closes = run_oracle(DATA2_SOURCE, bars, NQ, data2_closes=data2_closes, max_bars_back=0)
    dropped = run_oracle(DATA2_SOURCE, bars, NQ, max_bars_back=0)

    assert via_bars["text"] == via_closes["text"], (
        "python data2_bars= diverged from python data2_closes=: "
        f"{via_bars['fills']} fills sha {via_bars['sha256'][:12]} vs "
        f"{via_closes['fills']} fills sha {via_closes['sha256'][:12]}"
    )
    assert via_bars["sha256"] == via_closes["sha256"]
    assert via_bars["fills"] == via_closes["fills"]
    assert via_bars["text"] != dropped["text"], (
        "python data2_bars= matched the no-Data2 fallback — the argument "
        "was ignored"
    )
    assert err.getvalue() == ""
    print(
        f"  [ok] python data2_bars= == data2_closes= "
        f"({via_bars['fills']} fills, sha {via_bars['sha256'][:12]}); "
        f"not the Data1 fallback ({dropped['fills']} fills)"
    )


def _audit_tape_paths():
    el = os.environ.get("PB_AUDIT_EL", "")
    bars = os.environ.get("PB_AUDIT_BARS", "")
    d2 = os.environ.get("PB_AUDIT_D2", "")
    if not (el and bars and d2
            and os.path.exists(el) and os.path.exists(bars)
            and os.path.exists(d2)):
        return None
    return el, bars, d2


def _skip_audit_if_absent():
    """Return the reference audit tape paths, or skip.

    pytest is optional: the public tier runs this file as a stdlib script
    under ``/usr/bin/python3`` with no pytest installed. When pytest is the
    runner it is already in ``sys.modules`` and we use ``pytest.skip``.
    """
    paths = _audit_tape_paths()
    if paths is not None:
        return paths
    msg = AUDIT_TAPE_SKIP
    pytest_mod = sys.modules.get("pytest")
    if pytest_mod is not None:
        pytest_mod.skip(msg)
    print("  [SKIP] " + msg)
    return None


def test_reference_audit_python_data2_bars_matches_closes():
    """Audit window + full tape: data2_bars= must reproduce the canonical digest.

    Pre-fix this was 948 / 608c16f090.. (window) and 7964 fills (full tape),
    with empty stderr. After the fix both spellings match 928 / d8ad8dbbf1c7
    (window) and 7364 / d839e7ab (full tape).
    """
    paths = _skip_audit_if_absent()
    if paths is None:
        return
    el_path, bars_path, d2_path = paths
    el = open(el_path, encoding="utf-8").read()
    bars = load_bars(bars_path)
    data2 = load_bars(d2_path)
    window = AUDIT_REF_3_WINDOW
    w_bars, w_d2 = bars[window], data2[window]
    w_closes = [float(x) for x in ohlcv_columns(w_d2)["close"]]

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        via_bars = run_oracle(el, w_bars, NQ, data2_bars=w_d2, digest_version=1, max_bars_back=0)
    via_closes = run_oracle(el, w_bars, NQ, data2_closes=w_closes, digest_version=1, max_bars_back=0)

    assert via_bars["fills"] == AUDIT_REF_3_WINDOW_FILLS, (
        f"window data2_bars= gave {via_bars['fills']} fills "
        f"(silent-ignore was {AUDIT_REF_3_WINDOW_SILENT_FILLS}); "
        f"canonical is {AUDIT_REF_3_WINDOW_FILLS}"
    )
    assert via_bars["sha256"] == AUDIT_REF_3_WINDOW_SHA256, (
        f"window data2_bars= sha {via_bars['sha256']} "
        f"(silent-ignore was {AUDIT_REF_3_WINDOW_SILENT_SHA256}); "
        f"canonical is {AUDIT_REF_3_WINDOW_SHA256}"
    )
    assert via_bars["text"] == via_closes["text"]
    assert via_bars["sha256"] != AUDIT_REF_3_WINDOW_SILENT_SHA256
    assert err.getvalue() == ""

    full_closes = [float(x) for x in ohlcv_columns(data2)["close"]]
    full_bars = run_oracle(el, bars, NQ, data2_bars=data2, digest_version=1, max_bars_back=0)
    full_closes_d = run_oracle(el, bars, NQ, data2_closes=full_closes, digest_version=1, max_bars_back=0)
    assert full_bars["fills"] == AUDIT_REF_3_FILLS
    assert full_bars["sha256"] == AUDIT_REF_3_SHA256
    assert full_bars["text"] == AUDIT_REF_3_DIGEST
    assert full_bars["text"] == full_closes_d["text"]
    print(
        f"  [ok] reference audit window {via_bars['fills']} / "
        f"{via_bars['sha256'][:12]}; full tape {full_bars['fills']} / "
        f"{full_bars['sha256'][:12]}"
    )


def check_gt2_capture_oracle(max_bars=20000):
    """Capture-gated: GT2 persist digest is stable and matches run_el trades.

    Returns (ok, detail). Caller must only invoke this when captures are present.
    """
    from tests.test_general_entrypoint import _resolve_gt2_bars
    bars, _ = _resolve_gt2_bars(max_bars)
    el = open(GT2, encoding="utf-8").read()
    a = run_oracle(el, bars, NQ, max_bars_back=0)
    b = run_oracle(el, bars, NQ, max_bars_back=0)
    if a["text"] != b["text"]:
        return False, "two persist runs produced different digests"
    traced = run_el(el, bars, NQ, retain_rows=False, max_bars_back=0)
    if traced["trades"] != a["records"]:
        return False, (
            f"persist fills {a['fills']} != run_el trades {len(traced['trades'])}")
    return True, (
        f"GT2 {a['n_bars']} bars, {a['fills']} fills, "
        f"{a['round_turns']} round-turns, sha256={a['sha256']}")


def main():
    tests = [
        test_reference_audit_citation_constants,
        test_empty_tape_digest,
        test_count_strategy_needs_persist,
        test_hasher_matches_payload,
        test_trade_type_short_from_flat,
        test_trade_type_buy_to_cover,
        test_trade_type_reversal_splits,
        test_fills_csv_keeps_digest_and_matches_native,
        test_gt2_sample_digest_frozen,
        test_window_identity_and_disagreement,
        test_cli_digest_and_compare,
        test_cli_requires_bars,
        test_data2_closes_from_bars_aligns_on_date_time,
        test_python_path_consumes_data2_bars,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()

    print("running check_gt2_capture_oracle ...")
    if not capture_gate.captures_present():
        print("  " + capture_gate.skip_message("oracle GT2 capture digest"))
    else:
        ok, detail = check_gt2_capture_oracle()
        assert ok, detail
        print(f"  [ok] {detail}")

    print("running test_reference_audit_python_data2_bars_matches_closes ...")
    if _audit_tape_paths() is None:
        print("  [SKIP] " + AUDIT_TAPE_SKIP)
    else:
        test_reference_audit_python_data2_bars_matches_closes()

    print("\nALL ORACLE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _fill(side, price, contracts=1):
    return {"type": side, "signal": "s", "date": 1240102, "time": 930,
            "price": price, "contracts": contracts}


def test_trade_summary_fifo_long_short_and_drawdown():
    from pl_transpiler.tools.pl_oracle import (
        BUY_SIDE,
        SELL_SIDE,
        format_summary,
        trade_summary,
    )
    fills = [
        _fill(BUY_SIDE, 100.0, 2),   # long 2
        _fill(SELL_SIDE, 103.0),     # +3
        _fill(SELL_SIDE, 99.0),      # -1
        _fill(SELL_SIDE, 99.0),      # short 1
        _fill(BUY_SIDE, 104.0),      # -5
        _fill(BUY_SIDE, 104.0),      # long 1, left open
    ]
    s = trade_summary(fills, 20.0)
    assert s == {"trades": 3, "winners": 1, "losers": 2, "net_profit": -60.0,
                 "gross_profit": 60.0, "gross_loss": -120.0,
                 "max_drawdown": 120.0, "open_contracts": 1}
    text = format_summary(s)
    assert "  net profit:     -60.00\n" in text
    assert "  win rate:       33.3%\n" in text


def test_trade_summary_no_trades():
    from pl_transpiler.tools.pl_oracle import format_summary, trade_summary
    s = trade_summary([], 20.0)
    assert s["trades"] == 0 and s["net_profit"] == 0.0
    assert "  win rate:       n/a\n" in format_summary(s)


def test_summary_flag_stdout_unchanged_and_paths_agree():
    from pl_transpiler.tools.pl_oracle import main
    el = os.path.join(REPO, "examples", "ma_cross.el")
    csv = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

    def run(*extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            assert main([el, csv, "-q", *extra]) == 0
        return out.getvalue(), err.getvalue()

    plain_out, plain_err = run("--python")
    out, err = run("--python", "--summary")
    assert out == plain_out
    assert "performance summary" not in plain_err
    block = err[err.index("performance summary"):]
    assert "  trades:         1\n" in block
    if accel_available():
        n_out, n_err = run("--native", "--summary")
        assert n_out == out
        assert n_err[n_err.index("performance summary"):] == block
