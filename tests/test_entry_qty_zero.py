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

"""test_entry_qty_zero.py — a bare entry must not size itself off the position.

An order whose quantity is 0 (the EL source gave no explicit size) used to be
sized as ``self.contracts``. That is the OrderMode-4 EXIT rule ("close the whole
position"), and applying it to an ENTRY made an unguarded per-bar ``Buy`` double
the position every bar. Because ``_fill_at`` then emits one fill record per
contract, the run produced 2**n records — measured 2, 8, 32, 128, 512, 2048
fills as the tape grew 4, 6, 8, 10, 12, 14 bars — and OOM'd (SIGKILL, 11 GB RSS)
at ~200 bars. A 2.3M-bar reference run was unaffected only because its entries
never add to an existing position, which is what hid the bug.

A bare entry must add the default order size (1 contract). A bare exit must
still close the entire position.

Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.engine import FillEngine                              # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config           # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle                     # noqa: E402

NQ = get_config("NQ")

# A bare Buy with no explicit size, fired on every bar once the average is valid.
BARE_ENTRY_EL = (
    "Vars: F(0);\n"
    "F = Average(Close, 2);\n"
    'If F > 0 Then Buy ("LE") next bar at Market;\n'
)


def _bars(n, px=1.0):
    return [
        {"date": 1240102 + i, "time": 930 + (i % 60), "open": px, "high": px,
         "low": px, "close": px, "volume": 1.0}
        for i in range(n)
    ]


def test_bare_entry_does_not_pyramid():
    """Fill count grows linearly with tape length, not as 2**n."""
    counts = {}
    for n in (8, 16, 32, 64):
        counts[n] = run_oracle(BARE_ENTRY_EL, _bars(n), NQ)["fills"]
    # Decisive bound: fills stay <= bar count. The exponential bug produced
    # 2, 8, 32, 128, 512, 2048 on 4..14 bars, so 64 bars would have been 2**64.
    for n, c in counts.items():
        assert c <= n, f"fills must stay <= bars (was 2**n): {counts}"
    # 8x the tape must give roughly 8x the fills, not an explosion.
    assert counts[64] <= 12 * counts[8], f"fills growing super-linearly: {counts}"
    print(f"  [ok] bare entry does not pyramid: {counts}")


def test_bare_entry_adds_one_contract():
    """qty 0 on an entry adds ONE contract — it does not double the position."""
    fe = FillEngine(config=NQ)
    fe._fill_at(("buy", "market", 0, "next", "LE"), 100.0, 1)
    assert fe.contracts == 1, f"first bare entry gave {fe.contracts} contracts"
    fe._fill_at(("buy", "market", 0, "next", "LE"), 100.0, 2)
    assert fe.contracts == 2, f"second bare entry gave {fe.contracts} (doubling?)"
    fe._fill_at(("buy", "market", 0, "next", "LE"), 100.0, 3)
    assert fe.contracts == 3, f"third bare entry gave {fe.contracts} (doubling?)"
    print("  [ok] bare entry adds 1 contract (was: doubled the position)")


def test_bare_exit_still_closes_entire_position():
    """The OrderMode-4 exit rule must survive: qty 0 on a CLOSE flattens."""
    fe = FillEngine(config=NQ)
    for _ in range(3):
        fe._fill_at(("buy", "market", 0, "next", "LE"), 100.0, 1)
    assert fe.contracts == 3, fe.contracts
    fe._fill_at(("sell", "market", 0, "next", "LX"), 100.0, 2)
    assert fe.contracts == 0, f"bare exit must flatten the long, left {fe.contracts}"
    print("  [ok] bare exit (qty 0) still closes the whole position")


def test_bare_sellshort_entry_is_not_an_exit():
    """qty 0 on a short ENTRY is also 1 contract, not 'close everything'."""
    fe = FillEngine(config=NQ)
    fe._fill_at(("sellshort", "market", 0, "next", "SE"), 100.0, 1)
    # `contracts` is a magnitude; the side lives in `market_position`.
    assert fe.market_position == -1 and fe.contracts == 1, (fe.market_position, fe.contracts)
    fe._fill_at(("sellshort", "market", 0, "next", "SE"), 100.0, 2)
    assert fe.market_position == -1 and fe.contracts == 2, \
        f"bare short entry added {fe.contracts} (doubling?)"
    print("  [ok] bare short entry adds 1 contract")


def test_explicit_quantities_unaffected():
    fe = FillEngine(config=NQ)
    fe._fill_at(("buy", "market", 4, "next", "LE"), 100.0, 1)
    assert fe.contracts == 4, fe.contracts
    fe._fill_at(("sell", "market", 2, "next", "LX"), 100.0, 2)
    assert fe.contracts == 2, fe.contracts
    fe._fill_at(("buytocover", "market", 0, "next", "XC"), 100.0, 3)
    print("  [ok] explicit quantities unchanged")


def main():
    tests = [
        test_bare_entry_does_not_pyramid,
        test_bare_entry_adds_one_contract,
        test_bare_exit_still_closes_entire_position,
        test_bare_sellshort_entry_is_not_an_exit,
        test_explicit_quantities_unaffected,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL ENTRY-QTY-ZERO TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
