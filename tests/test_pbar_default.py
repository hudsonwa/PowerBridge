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

"""The .pbar sidecar is the DEFAULT load path — and it is never trusted blindly.

``load_bars(csv)`` prefers the sibling ``<stem>.pbar`` when one exists (opening a
2.3M-bar tape is ~0.006 s against ~3.0 s to parse the CSV, 545x). Three things
have to hold, and this file asserts all three:

  1. EQUIVALENCE — the sidecar yields exactly the CSV's values, field for field.
  2. FRESHNESS  — a sidecar OLDER than its CSV is refused, LOUDLY, and the CSV is
     parsed instead. A stale sidecar must never be a silent wrong answer.
  3. ESCAPE HATCH — ``PB_NO_PBAR=1`` forces the CSV path.

Stdlib only. No captures, no private tape.
"""
import contextlib
import io
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.bar_tape import ohlcv_columns  # noqa: E402
from pl_transpiler.tools.pl_run import csv_to_pbar, load_bars, pbar_for  # noqa: E402

CSV = (
    "date,time,open,high,low,close,volume\n"
    "1180102,831,15000.25,15010.5,14990.0,15005.75,1234\n"
    "1180102,832,15005.75,15020.0,15000.0,15018.5,987\n"
    "1180102,833,15018.5,15019.0,14980.25,14985.0,1500\n"
)

COLUMNS = ("open", "high", "low", "close", "volume", "date", "time")


def _cols(bars):
    c = ohlcv_columns(bars)
    return {k: list(c[k]) for k in COLUMNS}


def test_sidecar_is_equivalent_to_the_csv():
    with tempfile.TemporaryDirectory() as d:
        csvp = os.path.join(d, "bars.csv")
        with open(csvp, "w", encoding="utf-8") as f:
            f.write(CSV)

        from_csv = _cols(load_bars(csvp, prefer_pbar=False))
        csv_to_pbar(csvp, pbar_for(csvp))
        assert os.path.exists(pbar_for(csvp)), "sidecar was not written"

        # Freshness: the sidecar was just written, so it is not older.
        os.utime(pbar_for(csvp), (time.time() + 1, time.time() + 1))
        from_pbar = _cols(load_bars(csvp))
        assert from_pbar == from_csv, "sidecar values differ from the CSV"
        print("  [ok] sidecar is field-for-field identical to the CSV")


def test_stale_sidecar_is_refused_loudly():
    with tempfile.TemporaryDirectory() as d:
        csvp = os.path.join(d, "bars.csv")
        with open(csvp, "w", encoding="utf-8") as f:
            f.write(CSV)
        csv_to_pbar(csvp, pbar_for(csvp))

        # Make the CSV NEWER than the sidecar — the sidecar is now stale.
        future = time.time() + 60
        os.utime(csvp, (future, future))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            loaded = _cols(load_bars(csvp))
        assert loaded == _cols(load_bars(csvp, prefer_pbar=False)), "stale load differs"
        msg = err.getvalue()
        assert "OLDER" in msg and "bars.pbar" in msg, (
            "a stale sidecar must be refused with a stderr note, got %r" % msg)
        print("  [ok] stale sidecar refused, loudly: %s" % msg.strip()[:72])


def test_pb_no_pbar_forces_the_csv():
    with tempfile.TemporaryDirectory() as d:
        csvp = os.path.join(d, "bars.csv")
        with open(csvp, "w", encoding="utf-8") as f:
            f.write(CSV)
        csv_to_pbar(csvp, pbar_for(csvp))
        os.utime(pbar_for(csvp), (time.time() + 1, time.time() + 1))

        os.environ["PB_NO_PBAR"] = "1"
        try:
            forced = _cols(load_bars(csvp))
        finally:
            os.environ.pop("PB_NO_PBAR", None)
        assert forced == _cols(load_bars(csvp, prefer_pbar=False))
        print("  [ok] PB_NO_PBAR=1 forces the CSV parse")


def main():
    for t in (test_sidecar_is_equivalent_to_the_csv,
              test_stale_sidecar_is_refused_loudly,
              test_pb_no_pbar_forces_the_csv):
        print("running %s ..." % t.__name__)
        t()
    print("\nALL PBAR-DEFAULT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
