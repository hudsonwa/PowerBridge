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
bar_tape.py — compact column-contiguous OHLCV tape.

On-disk ``.pbar`` is a little-endian struct-of-arrays dump of the *same*
values ``load_bars_csv`` produces: EL-integer date/time and IEEE-754
**binary64** OHLC/volume. Storing float32 would change values and is
rejected. ``mmap`` is the default reader so a sweep can share one mapping.

Canonical in-memory form is the same layout (one contiguous array per
field). The Python path may index a bar as a dict (lazy view) for callers
that still walk ``list[dict]``; ``run_el`` / ``pl_oracle`` consume the
columns directly and do not box a dict per bar.

Series history buffers stay growing Python lists — this module owns
*bar input*, not indicator/history series.

CSV remains the default. Nothing here is imported by ``load_bars_csv``.
"""
from __future__ import annotations

import argparse
import mmap
import os
import struct
import sys
from array import array
from collections.abc import Sequence

MAGIC = b"PBARS01\n"
VERSION = 1
FLAGS_NONE = 0
HEADER_FMT = "<8sIIQ"  # magic, version, flags, n_bars
HEADER_SIZE = struct.calcsize(HEADER_FMT)
# date i32, time i32, open/high/low/close/volume f64
_I32 = 4
_F64 = 8
BYTES_PER_BAR = _I32 + _I32 + 5 * _F64  # 48


def _require_little_endian():
    if sys.byteorder != "little":
        raise OSError(
            "PBARS01 is little-endian; this host is %s" % sys.byteorder
        )


def _require_native_sizes():
    if array("i").itemsize != _I32 or array("d").itemsize != _F64:
        raise RuntimeError(
            "native array('i')/%s or array('d')/%s is not 4/8 bytes"
            % (array("i").itemsize, array("d").itemsize)
        )


class BarTape(Sequence):
    """Column-contiguous OHLCV tape.

    Indexing with an int yields a dict with the same keys/types as
    ``load_bars_csv`` (lazy view). Slicing yields another BarTape over the
    same backing store (no copy). Columns are public attributes:
    ``date``, ``time``, ``open``, ``high``, ``low``, ``close``, ``volume``.
    """

    __slots__ = (
        "date", "time", "open", "high", "low", "close", "volume", "_mm",
    )

    def __init__(self, *, date, time, open, high, low, close, volume, _mm=None):
        n = len(date)
        if not (len(time) == len(open) == len(high) == len(low)
                == len(close) == len(volume) == n):
            raise ValueError("BarTape column lengths differ")
        self.date = date
        self.time = time
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self._mm = _mm

    @property
    def n(self):
        return len(self.date)

    def __len__(self):
        return len(self.date)

    def __repr__(self):
        return "BarTape(n=%d)" % len(self)

    def release(self):
        """Drop column views and close the mmap, if any.

        Column attributes become empty tuples so the mapping is no longer
        exported. Sibling slice views keep the mapping alive until they
        are released too.
        """
        mm = self._mm
        self._mm = None
        self.date = ()
        self.time = ()
        self.open = ()
        self.high = ()
        self.low = ()
        self.close = ()
        self.volume = ()
        if mm is None:
            return
        try:
            mm.close()
        except BufferError:
            # Another BarTape slice still exports this mapping.
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            if step != 1:
                raise ValueError("BarTape slice step must be 1")
            return BarTape(
                date=self.date[start:stop],
                time=self.time[start:stop],
                open=self.open[start:stop],
                high=self.high[start:stop],
                low=self.low[start:stop],
                close=self.close[start:stop],
                volume=self.volume[start:stop],
                _mm=self._mm,
            )
        i = int(key)
        n = len(self)
        if i < 0:
            i += n
        if i < 0 or i >= n:
            raise IndexError(key)
        return {
            "date": int(self.date[i]),
            "time": int(self.time[i]),
            "open": float(self.open[i]),
            "high": float(self.high[i]),
            "low": float(self.low[i]),
            "close": float(self.close[i]),
            "volume": float(self.volume[i]),
        }

    def as_dicts(self):
        """Eager list[dict] copy. Prefer columns for a run."""
        return [self[i] for i in range(len(self))]


def ohlcv_columns(bars):
    """Unpack bars into column dict ``n, open, high, low, close, volume, date, time``.

    BarTape: the existing column views (no copy).
    Sequence of dicts: new lists, same as today's ``run_el`` / ``run_oracle``.
    """
    if isinstance(bars, BarTape):
        return {
            "n": len(bars),
            "open": bars.open,
            "high": bars.high,
            "low": bars.low,
            "close": bars.close,
            "volume": bars.volume,
            "date": bars.date,
            "time": bars.time,
        }
    return {
        "n": len(bars),
        "open": [float(b["open"]) for b in bars],
        "high": [float(b["high"]) for b in bars],
        "low": [float(b["low"]) for b in bars],
        "close": [float(b["close"]) for b in bars],
        "volume": [float(b["volume"]) for b in bars],
        "date": [int(b["date"]) for b in bars],
        "time": [int(b["time"]) for b in bars],
    }


def _as_array(col, typecode, n):
    if isinstance(col, array) and col.typecode == typecode and len(col) == n:
        return col
    out = array(typecode, col)
    if len(out) != n:
        raise ValueError("column length %d != n=%d" % (len(out), n))
    return out


def write_pbar(path, bars):
    """Write ``bars`` (BarTape or sequence of dicts) as a ``.pbar`` file.

    Values are stored as parsed: int32 EL date/time, float64 OHLC/volume.
    Returns the bar count.
    """
    _require_little_endian()
    _require_native_sizes()
    cols = ohlcv_columns(bars)
    n = cols["n"]
    date = _as_array(cols["date"], "i", n)
    time = _as_array(cols["time"], "i", n)
    open_ = _as_array(cols["open"], "d", n)
    high = _as_array(cols["high"], "d", n)
    low = _as_array(cols["low"], "d", n)
    close = _as_array(cols["close"], "d", n)
    volume = _as_array(cols["volume"], "d", n)
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, FLAGS_NONE, n)
    with open(path, "wb") as f:
        f.write(header)
        date.tofile(f)
        time.tofile(f)
        open_.tofile(f)
        high.tofile(f)
        low.tofile(f)
        close.tofile(f)
        volume.tofile(f)
    return n


def load_pbar(path):
    """Memory-map a ``.pbar`` into a BarTape (zero-copy columns).

    The returned tape holds the ``mmap`` object; keep it alive while using
    the columns, or use it as a context manager (``release()`` on exit).
    """
    _require_little_endian()
    _require_native_sizes()
    fd = open(path, "rb")
    try:
        size = os.fstat(fd.fileno()).st_size
        if size < HEADER_SIZE:
            raise ValueError(
                "pbar %r is %d bytes; need at least a %d-byte header"
                % (path, size, HEADER_SIZE)
            )
        mm = mmap.mmap(fd.fileno(), 0, access=mmap.ACCESS_READ)
    finally:
        fd.close()
    try:
        magic, version, flags, n = struct.unpack_from(HEADER_FMT, mm, 0)
        if magic != MAGIC:
            raise ValueError("bad pbar magic %r (want %r)" % (magic, MAGIC))
        if version != VERSION:
            raise ValueError("unsupported pbar version %d" % version)
        if flags != FLAGS_NONE:
            raise ValueError("unsupported pbar flags 0x%x" % flags)
        n = int(n)
        expected = HEADER_SIZE + n * BYTES_PER_BAR
        if len(mm) != expected:
            raise ValueError(
                "pbar size %d != expected %d for n=%d" % (len(mm), expected, n)
            )
        mv = memoryview(mm)
        off = HEADER_SIZE

        def _take(nbytes, fmt):
            nonlocal off
            sl = mv[off:off + nbytes]
            off += nbytes
            return sl.cast(fmt)

        date = _take(n * _I32, "i")
        time = _take(n * _I32, "i")
        open_ = _take(n * _F64, "d")
        high = _take(n * _F64, "d")
        low = _take(n * _F64, "d")
        close = _take(n * _F64, "d")
        volume = _take(n * _F64, "d")
        if off != expected:
            raise ValueError("pbar column cursor %d != %d" % (off, expected))
        return BarTape(
            date=date, time=time, open=open_, high=high, low=low,
            close=close, volume=volume, _mm=mm,
        )
    except Exception:
        mm.close()
        raise


def csv_to_pbar(csv_path, pbar_path):
    """Parse an OHLCV CSV with ``load_bars_csv`` and write a ``.pbar``.

    Same parse, same float64 values. Returns the bar count.
    """
    from pl_transpiler.tools.pl_run import load_bars_csv
    return write_pbar(pbar_path, load_bars_csv(csv_path))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m pl_transpiler.runtime.bar_tape",
        description="Convert an OHLCV CSV to a compact little-endian .pbar "
                    "(column-contiguous float64 + EL int date/time).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "csv2pbar",
        help="write a .pbar from a CSV using the same parse as load_bars_csv")
    p.add_argument("csv_path", help="OHLCV CSV (date,time,open,high,low,close,volume)")
    p.add_argument("pbar_path", help="output .pbar path")
    args = ap.parse_args(argv)
    if args.cmd == "csv2pbar":
        n = csv_to_pbar(args.csv_path, args.pbar_path)
        print("[bar_tape] wrote %d bars -> %s" % (n, args.pbar_path),
              file=sys.stderr)
        return 0
    ap.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
