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
test_bar_tape.py — compact .pbar bar input (stdlib mmap / array).

CSV remains the default. A .pbar stores the same values load_bars_csv
produces (EL-int date/time, float64 OHLC/volume — never float32). Whole-tape
value equality is exact (no tolerance). run_el / pl_oracle accept a BarTape
and must match the dict-list path's trade list.

Stdlib only, no captures.
"""
import csv
import os
import struct
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.bar_tape import (  # noqa: E402
    BYTES_PER_BAR,
    HEADER_FMT,
    HEADER_SIZE,
    MAGIC,
    VERSION,
    BarTape,
    csv_to_pbar,
    load_pbar,
    ohlcv_columns,
    write_pbar,
)
from pl_transpiler.runtime.bar_tape import (
    main as tape_main,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.runtime.pl_runtime import f32  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle, window_bars  # noqa: E402
from pl_transpiler.tools.pl_run import (
    csv_to_pbar as run_csv_to_pbar,
)
from pl_transpiler.tools.pl_run import (  # noqa: E402
    load_bars,
    load_bars_csv,
    resolve_trade_from,
    run_el,
)

NQ = get_config("NQ")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")

COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 3 Then Buy (\"LE\") next bar at market;\n"
)


def _same_f64(a, b):
    return struct.pack("d", float(a)) == struct.pack("d", float(b))


def _bar_equal(a, b):
    return (
        int(a["date"]) == int(b["date"])
        and int(a["time"]) == int(b["time"])
        and _same_f64(a["open"], b["open"])
        and _same_f64(a["high"], b["high"])
        and _same_f64(a["low"], b["low"])
        and _same_f64(a["close"], b["close"])
        and _same_f64(a["volume"], b["volume"])
    )


def _assert_whole_tape_equal(csv_bars, tape):
    assert len(tape) == len(csv_bars), (len(tape), len(csv_bars))
    for i, csv_bar in enumerate(csv_bars):
        pbar_bar = tape[i]
        assert _bar_equal(csv_bar, pbar_bar), (
            "bar %d csv=%r pbar=%r" % (i, csv_bar, pbar_bar)
        )
        # Types match the CSV parse: int date/time, float (binary64) OHLC/V.
        for k in ("date", "time"):
            assert type(pbar_bar[k]) is int, (k, type(pbar_bar[k]))
        for k in ("open", "high", "low", "close", "volume"):
            assert type(pbar_bar[k]) is float, (k, type(pbar_bar[k]))


def test_sample_csv_roundtrip_whole_tape():
    csv_bars = load_bars_csv(SAMPLE)
    assert csv_bars, "shipped sample must be non-empty"
    with tempfile.TemporaryDirectory() as td:
        pbar_path = os.path.join(td, "sample.pbar")
        n = csv_to_pbar(SAMPLE, pbar_path)
        assert n == len(csv_bars)
        assert os.path.getsize(pbar_path) == HEADER_SIZE + n * BYTES_PER_BAR
        with load_pbar(pbar_path) as tape:
            _assert_whole_tape_equal(csv_bars, tape)
            via_load_bars = load_bars(pbar_path)
            _assert_whole_tape_equal(csv_bars, via_load_bars)
            via_load_bars.release()
    print("  [ok] shipped sample CSV <-> .pbar whole-tape value equality (%d bars)"
          % len(csv_bars))


def test_synthetic_whole_tape_mixed_formats_and_not_f32():
    """Exhaustive equality on a generated tape; 0.1 stays binary64, not f32."""
    rows = []
    # Mix EL-int and MultiCharts ASCII date/time forms.
    rows.append(("1240102", "930", "15000.00", "15005.00", "14992.50", "15000.25", "125"))
    rows.append(("02/01/2024", "09:31:00", "0.1", "1e2", "-3.5", "0.0", "0"))
    rows.append(("1240102", "09:32:00", "1.0000000000000002", "2.5", "0.25", "0.125", "1.5"))
    for i in range(3, 2500):
        px = 1000.0 + i * 0.25
        rows.append((
            str(1240102 + (i // 400)),
            # Oldest first (the loader refuses bars out of order): minute
            # 09:30 + (i % 400) of day i // 400.
            str((570 + i % 400) // 60 * 100 + (570 + i % 400) % 60),
            "%.10f" % (px + 0.1),
            "%.10f" % (px + 0.5),
            "%.10f" % px,
            "%.10f" % (px + 0.25),
            str(i),
        ))
    with tempfile.TemporaryDirectory() as td:
        csv_path = os.path.join(td, "mix.csv")
        pbar_path = os.path.join(td, "mix.pbar")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "time", "open", "high", "low", "close", "volume"])
            w.writerows(rows)
        csv_bars = load_bars_csv(csv_path)
        n = run_csv_to_pbar(csv_path, pbar_path)
        assert n == len(csv_bars) == len(rows)
        with load_pbar(pbar_path) as tape:
            _assert_whole_tape_equal(csv_bars, tape)
            # Fidelity: CSV "0.1" is binary64 0.1, NOT f32(0.1).
            tenth = tape[1]["open"]
            assert _same_f64(tenth, 0.1)
            assert not _same_f64(tenth, f32(0.1))
            assert _same_f64(tenth, csv_bars[1]["open"])
    print("  [ok] synthetic %d-bar whole-tape equality; 0.1 is float64 not f32"
          % len(rows))


def test_empty_tape_and_fail_loud():
    with tempfile.TemporaryDirectory() as td:
        empty_path = os.path.join(td, "empty.pbar")
        n = write_pbar(empty_path, [])
        assert n == 0
        with load_pbar(empty_path) as tape:
            assert len(tape) == 0
            assert tape.as_dicts() == []
            try:
                tape[0]
                raise AssertionError("expected IndexError on empty tape")
            except IndexError:
                pass

        bad_magic = os.path.join(td, "bad.pbar")
        with open(bad_magic, "wb") as f:
            f.write(b"NOPE0000" + b"\x00" * (HEADER_SIZE - 8))
        try:
            load_pbar(bad_magic)
            raise AssertionError("expected bad magic to raise")
        except ValueError as e:
            assert "magic" in str(e).lower()

        trunc = os.path.join(td, "trunc.pbar")
        with open(trunc, "wb") as f:
            f.write(struct.pack(HEADER_FMT, MAGIC, VERSION, 0, 3))  # claims 3 bars
        try:
            load_pbar(trunc)
            raise AssertionError("expected truncated pbar to raise")
        except ValueError as e:
            assert "size" in str(e).lower() or "small" in str(e).lower()

        bad_ver = os.path.join(td, "ver.pbar")
        with open(bad_ver, "wb") as f:
            f.write(struct.pack(HEADER_FMT, MAGIC, VERSION + 1, 0, 0))
        try:
            load_pbar(bad_ver)
            raise AssertionError("expected bad version to raise")
        except ValueError as e:
            assert "version" in str(e).lower()
    print("  [ok] empty tape + fail-loud magic/size/version")


def test_slice_is_view_and_resolve_trade_from():
    csv_bars = load_bars_csv(SAMPLE)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "s.pbar")
        write_pbar(path, csv_bars)
        with load_pbar(path) as tape:
            mid = len(tape) // 2
            sl = tape[mid:]
            assert isinstance(sl, BarTape)
            assert len(sl) == len(tape) - mid
            assert _bar_equal(sl[0], csv_bars[mid])
            sl.release()
            assert resolve_trade_from(tape, from_bar=mid) == mid
            d0 = int(tape.date[0])
            assert resolve_trade_from(tape, from_date=d0) == 0
            assert resolve_trade_from(csv_bars, from_date=d0) == 0
            past = resolve_trade_from(tape, from_date=d0 + 10 ** 6)
            assert past == len(tape)
    dated = [
        {"date": 1240100 + i, "time": 930, "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0}
        for i in range(8)
    ]
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "d.pbar")
        write_pbar(path, dated)
        with load_pbar(path) as tape:
            sliced, extra = window_bars(
                tape, from_date=1240103, to_date=1240105,
                extra=list(range(8)))
            assert isinstance(sliced, BarTape)
            assert [int(d) for d in sliced.date] == [1240103, 1240104, 1240105]
            assert extra == [3, 4, 5]
            sliced.release()
            assert resolve_trade_from(tape, from_date=1240103) == 3
            assert resolve_trade_from(dated, from_date=1240103) == 3
    print("  [ok] BarTape slice is a view; resolve_trade_from / window_bars match dicts")


def test_run_el_and_oracle_match_dict_path():
    el = open(GT2, encoding="utf-8").read()
    csv_bars = load_bars_csv(SAMPLE)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "gt2.pbar")
        write_pbar(path, csv_bars)
        with load_pbar(path) as tape:
            dict_out = run_el(el, csv_bars, NQ, retain_rows=False)
            tape_out = run_el(el, tape, NQ, retain_rows=False)
            assert tape_out["trades"] == dict_out["trades"]
            d_csv = run_oracle(el, csv_bars, NQ)
            d_pbar = run_oracle(el, tape, NQ)
            assert d_pbar["text"] == d_csv["text"]
            d_count = run_oracle(COUNT_EL, tape, NQ)
            assert d_count["fills"] == run_oracle(COUNT_EL, csv_bars, NQ)["fills"]
    print("  [ok] run_el trades and oracle digest identical on BarTape vs dicts")


def test_load_bars_csv_unchanged_and_cli_csv2pbar():
    """CSV loader still returns list[dict]; dispatcher only switches on .pbar."""
    bars = load_bars_csv(SAMPLE)
    assert type(bars) is list
    assert type(bars[0]) is dict
    also = load_bars(SAMPLE)
    assert also == bars
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "cli.pbar")
        rc = tape_main(["csv2pbar", SAMPLE, out])
        assert rc == 0
        with load_pbar(out) as tape:
            _assert_whole_tape_equal(bars, tape)
    cols = ohlcv_columns(bars)
    assert cols["n"] == len(bars)
    assert cols["close"][0] == float(bars[0]["close"])
    print("  [ok] load_bars_csv still list[dict]; csv2pbar CLI roundtrips")


def test_columns_not_copied_from_bartape():
    csv_bars = load_bars_csv(SAMPLE)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "id.pbar")
        write_pbar(path, csv_bars)
        with load_pbar(path) as tape:
            cols = ohlcv_columns(tape)
            assert cols["n"] == len(tape)
            assert cols["close"] is tape.close
            assert cols["date"] is tape.date
            del cols
    print("  [ok] ohlcv_columns(BarTape) is a view, not a copy")


def main():
    tests = [
        test_sample_csv_roundtrip_whole_tape,
        test_synthetic_whole_tape_mixed_formats_and_not_f32,
        test_empty_tape_and_fail_loud,
        test_slice_is_view_and_resolve_trade_from,
        test_run_el_and_oracle_match_dict_path,
        test_load_bars_csv_unchanged_and_cli_csv2pbar,
        test_columns_not_copied_from_bartape,
    ]
    for t in tests:
        print("running %s ..." % t.__name__)
        t()
    print("\nALL BAR-TAPE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
