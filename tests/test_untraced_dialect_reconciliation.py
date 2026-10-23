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
test_untraced_dialect_reconciliation.py — pin the three-way dialect counts.

This test settles the withdrawn "untraced emits 0 trades" claim against the
reference strategy. The three dialect modes are:

  traced            transpile(trace=True)            -> run_el          (fills N)
  clean mirror      transpile(dialect="mirror")      -> refused by run_bar_loop
  runnable untraced transpile(trace=False, persist=True) -> run_oracle  (fills N)

The finding is pinned in two legs:

  1. A portable stateful-gate program (no captures, no audit tape) asserts
     the runnable untraced dialect AGREES with the traced dialect (identical
     fills, identical digest) while the clean mirror dialect is refused by the bar loop.

  2. An audit-tape leg (the reference audit EL over the 2,322,973-bar NQ tape with
     aligned Data2) asserts trace == persist == AUDIT_REF_3 (7364 fills /
     3682 round-turns / d839e7ab…), and mirror == 0. This leg SKIPs when the
     audit files (not shipped) are absent, exactly like the capture-gated gates.

Stdlib only. No captures required for the portable leg.
"""
import contextlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.engine import FillEngine, run_bar_loop  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import (  # noqa: E402
    AUDIT_REF_3_DIGEST,
    AUDIT_REF_3_FILLS,
    AUDIT_REF_3_ROUND_TURNS,
    fills_sha256,
    run_oracle,
)
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import AUDIT_TAPE_SKIP  # noqa: E402

NQ = get_config("NQ")


# --- portable stateful gate -------------------------------------------------

GATE_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 2 Then Buy (\"LE\") next bar at market;\n"
    "If Count = 4 Then Sell (\"LX\") next bar at market;\n"
)

_GATE_BARS = [(1.0, 1.2, 0.8, 1.0, 1.0)] * 6
_GATE_DATES = [1240102] * 6
_GATE_TIMES = [930 + i for i in range(6)]


def _fills_for(mode, el=GATE_EL):
    """Transpile GATE_EL in `mode` and run it; return (fills, round_turns, sha256)."""
    if mode == "trace":
        py = transpile(el, trace=True)
        emit_trace = True
    elif mode == "persist":
        py = transpile(el, trace=False, persist=True)
        emit_trace = False
    elif mode == "mirror":
        py = transpile(el, trace=False, persist=False)
        emit_trace = False
    else:
        raise ValueError(mode)
    ns = {}
    exec(compile(py, "<recon>", "exec"), ns)
    strategy = ns["strategy"]
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    run_bar_loop(
        strategy,
        n_bars=len(_GATE_BARS),
        get_ohlcv=lambda i: _GATE_BARS[i],
        dates=_GATE_DATES,
        times=_GATE_TIMES,
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *_: None,
        strict=True,
        emit_trace=emit_trace,
    )
    recs = fe.get_mc_trades()
    return len(recs), len(recs) // 2, fills_sha256(recs)


def test_three_way_portable_gate():
    traced = _fills_for("trace")
    persist = _fills_for("persist")
    try:
        _fills_for("mirror")
    except ValueError as exc:
        assert "mirror-dialect" in str(exc), exc
    else:
        raise AssertionError("bar loop must refuse the mirror dialect")

    # The stateful counter arms Buy on bar 2 and Sell on bar 4 -> 2 fills,
    # 1 round-turn. The runnable untraced dialect must AGREE with traced.
    assert traced == (2, 1, traced[2]), traced
    assert persist == traced, (
        f"runnable untraced ({persist}) must agree with traced ({traced})")
    print(f"  [ok] three-way: trace={traced[:2]} persist={persist[:2]} mirror=refused")


def test_transpile_mode_signatures():
    """The three modes emit exactly the documented strategies."""
    trace_py = transpile(GATE_EL, trace=True)
    persist_py = transpile(GATE_EL, trace=False, persist=True)
    mirror_py = transpile(GATE_EL, trace=False, persist=False)
    assert transpile(GATE_EL) == persist_py, "default must equal runnable persist"
    assert transpile(GATE_EL, dialect="mirror") == mirror_py
    assert "_state" in trace_py and "_trace" in trace_py
    assert "_state" in persist_py and "_trace" not in persist_py
    assert "_state" not in mirror_py and "_trace" not in mirror_py
    print("  [ok] trace=_state+_trace, persist=_state, mirror=neither; default=persist")


# --- audit-tape leg (not shipped) -------------------------------------------

# Paths to the reference audit tape come from the environment; the tape is
# not shipped with this repository:
#   PB_AUDIT_EL, PB_AUDIT_BARS, PB_AUDIT_D2
AUDIT_EL = os.environ.get("PB_AUDIT_EL", "")
AUDIT_BARS = os.environ.get("PB_AUDIT_BARS", "")
AUDIT_D2 = os.environ.get("PB_AUDIT_D2", "")


def _audit_files_present():
    return all(os.path.exists(p) for p in (AUDIT_EL, AUDIT_BARS, AUDIT_D2))


def _load_data2_closes(path):
    import csv
    closes = []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        hdr = [h.strip().lower() for h in next(r)]
        idx = hdr.index("close")
        for row in r:
            if row:
                closes.append(float(row[idx]))
    return closes


def _run_trace_mode(el, bars, d2):
    """Trace-mode (transpile trace=True) reference audit run: fills/digest.

    run_el has no Data2 parameter, so this mirrors run_oracle's wiring with
    transpile(trace=True) + emit_trace=True (the only difference from the
    persist min-emit run)."""
    from pl_transpiler.runtime.bar_tape import ohlcv_columns
    py = transpile(el, trace=True)
    ns = {}
    exec(compile(py, "<audit-trace>", "exec"), ns)
    fn = ns["strategy"]
    cols = ohlcv_columns(bars)
    n = cols["n"]
    o, h, l = cols["open"], cols["high"], cols["low"]
    c, v = cols["close"], cols["volume"]
    dates, times = cols["date"], cols["time"]
    fe = FillEngine(config=NQ)
    fe._emit_gta6 = False
    def extra(i):
        return {"data2_close": d2[i]}
    def goh(i):
        return (o[i], h[i], l[i], c[i], v[i])
    with open(os.devnull, "w", encoding="utf-8") as dn, \
            contextlib.redirect_stdout(dn):
        run_bar_loop(
            fn, n_bars=n, get_ohlcv=goh, dates=dates, times=times, config=NQ,
            fill_engine=fe, emit_row=lambda *_: None, per_bar_extra=extra,
            strict=True, trade_from=0, emit_trace=True,
        )
    # v1 log: the AUDIT_REF_3 pins are v1 digests.
    recs = fe.get_mc_trades(v1=True)
    return len(recs), len(recs) // 2, fills_sha256(recs, 1)


def check_audit_tape_reconciliation():
    """Full-tape three-way on the reference audit. Return (ok, detail). Audit-tape gated."""
    if not _audit_files_present():
        return False, AUDIT_TAPE_SKIP
    el = open(AUDIT_EL, encoding="utf-8").read()
    bars = load_bars_csv(AUDIT_BARS)
    d2 = _load_data2_closes(AUDIT_D2)

    # runnable untraced (persist=True) via the oracle's own path.
    oracle = run_oracle(el, bars, NQ, data2_closes=d2, digest_version=1)
    # traced (trace=True).
    trace_fills, trace_rt, trace_sha = _run_trace_mode(el, bars, d2)

    if oracle["fills"] != AUDIT_REF_3_FILLS or oracle["round_turns"] != AUDIT_REF_3_ROUND_TURNS:
        return False, f"oracle {oracle['fills']}/{oracle['round_turns']} != reference audit const"
    if oracle["text"] != AUDIT_REF_3_DIGEST:
        return False, "oracle digest diverged from AUDIT_REF_3_DIGEST"
    if (trace_fills, trace_rt, trace_sha) != (oracle["fills"], oracle["round_turns"], oracle["sha256"]):
        return False, (
            f"trace {trace_fills}/{trace_rt}/{trace_sha[:12]} != "
            f"persist {oracle['fills']}/{oracle['round_turns']}/{oracle['sha256'][:12]}")

    return True, (
        f"reference audit {oracle['n_bars']} bars: trace == persist == "
        f"{oracle['fills']} fills / {oracle['round_turns']} round-turns, "
        f"sha256={oracle['sha256'][:12]}")


def main():
    for t in (
        test_three_way_portable_gate,
        test_transpile_mode_signatures,
    ):
        print(f"running {t.__name__} ...")
        t()

    print("running check_audit_tape_reconciliation ...")
    if not _audit_files_present():
        print("  [SKIP] " + AUDIT_TAPE_SKIP)
    else:
        ok, detail = check_audit_tape_reconciliation()
        assert ok, detail
        print(f"  [ok] {detail}")

    print("\nALL UNTRACED-DIALECT RECONCILIATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
