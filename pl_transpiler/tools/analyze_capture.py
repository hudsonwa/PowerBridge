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

"""analyze_capture.py — stream a GTA5 (or any GT) capture CSV and report trade activity
and column health WITHOUT loading the file into memory. Built for the 600MB+ GTA5 dumps.

Single streaming pass, O(1) memory: uses csv.reader row-by-row, never reads the whole file.

It answers the question "did the strategy trade?" deterministically from three signals:
  - the `totaltrades` column  = MC's OWN cumulative trade count (last row = official total),
  - the `ordertag` column     = which entry/exit was issued on each bar,
  - the `marketpos` column     = position state, whose transitions are entries/exits.
and sanity-checks the capture: column-count consistency, Data2 columns populated, repeated
header rows (the Print(File()) appender concatenates runs if the file wasn't deleted first).

Usage:  python pl_transpiler/tools/analyze_capture.py <capture.csv>
Exit:   0 if the file parsed; 2 on usage/IO error. (It reports findings; it does not judge pass/fail.)
"""
import csv, os, sys
from collections import Counter


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def main(path):
    if not os.path.exists(path):
        print(f"ERROR: no such file: {path}")
        return 2
    size = os.path.getsize(path)
    csv.field_size_limit(1 << 24)
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rdr = csv.reader(fh)
        try:
            header = next(rdr)
        except StopIteration:
            print("ERROR: file is empty")
            return 2
        col = {name: i for i, name in enumerate(header)}
        ncol = len(header)

        def idx(*names):
            for n in names:
                if n in col:
                    return col[n]
            return None

        i_tag = idx("ordertag")
        i_mp = idx("marketpos")
        i_cc = idx("curcontracts")
        i_tt = idx("totaltrades")
        i_np = idx("netprofit")
        i_d2 = idx("d2close", "d2c")
        i_bn = idx("bn")
        i_date = idx("date")
        i_time = idx("time")

        nrows = 0
        badcols = 0
        dup_headers = 0
        distinct_bars = 0      # bn is monotonic within a pass; count transitions
        bn_resets = 0          # bn decreasing => a concatenated re-run pass
        prev_bn = None
        tags = Counter()
        mp_values = set()
        mp_changes = 0
        bars_in_pos = 0
        prev_mp = None
        d2_nonzero = 0
        d2_zero_or_blank = 0
        cc_max = None
        tt_last = None
        np_last = None
        first_stamp = last_stamp = None

        for row in rdr:
            if len(row) != ncol:
                # a row that exactly repeats the header = an appended re-run, not corruption
                if row == header:
                    dup_headers += 1
                    continue
                badcols += 1
                continue
            if i_tag is not None and row == header:
                dup_headers += 1
                continue
            nrows += 1

            if i_bn is not None:
                stamp = (row[i_bn],
                         row[i_date] if i_date is not None else "",
                         row[i_time] if i_time is not None else "")
                if first_stamp is None:
                    first_stamp = stamp
                last_stamp = stamp
                bn = num(row[i_bn])
                if bn is not None:
                    if bn != prev_bn:
                        distinct_bars += 1
                    if prev_bn is not None and bn < prev_bn:
                        bn_resets += 1
                    prev_bn = bn

            if i_tag is not None:
                t = row[i_tag].strip()
                if t:
                    tags[t] += 1

            if i_mp is not None:
                mp = num(row[i_mp])
                if mp is not None:
                    mp_values.add(int(mp) if mp == int(mp) else mp)
                    if mp != 0:
                        bars_in_pos += 1
                    if prev_mp is not None and mp != prev_mp:
                        mp_changes += 1
                    prev_mp = mp

            if i_cc is not None:
                cc = num(row[i_cc])
                if cc is not None:
                    cc_max = cc if cc_max is None else max(cc_max, cc)

            if i_d2 is not None:
                d2 = num(row[i_d2])
                if d2 is None or d2 == 0:
                    d2_zero_or_blank += 1
                else:
                    d2_nonzero += 1

            if i_tt is not None:
                v = num(row[i_tt])
                if v is not None:
                    tt_last = v
            if i_np is not None:
                v = num(row[i_np])
                if v is not None:
                    np_last = v

    mb = size / (1024 * 1024)
    print(f"file            : {path}  ({mb:.1f} MB)")
    print(f"columns         : {ncol}  ({'OK 90-col layout' if ncol == 90 else 'EXPECTED 90 - check header'})")
    print(f"data rows        : {nrows:,}")
    if i_bn is not None and distinct_bars:
        ratio = nrows / distinct_bars
        note = ""
        if ratio > 1.05:
            note = (f"  <-- {ratio:.2f}x rows/bar: MC recalc/intrabar DUPLICATION; "
                    f"dedup to one-row-per-bar (keep last) for clean ground truth")
        print(f"distinct bars    : {distinct_bars:,}{note}")
        if bn_resets:
            print(f"bn resets        : {bn_resets}  (bn decreased - concatenated re-run passes in the file)")
    if first_stamp:
        print(f"first bar        : bn={first_stamp[0]} date={first_stamp[1]} time={first_stamp[2]}")
        print(f"last  bar        : bn={last_stamp[0]} date={last_stamp[1]} time={last_stamp[2]}")
    if dup_headers:
        print(f"repeated headers : {dup_headers}  (file was NOT deleted before a re-run - multiple runs concatenated; "
              f"delete C:\\pl_gt\\<file> and re-run for a clean single capture)")
    if badcols:
        print(f"malformed rows   : {badcols}  (field count != {ncol} - investigate)")

    print("\n--- TRADE ACTIVITY ---")
    if i_tt is not None:
        verdict = "TRADED" if (tt_last or 0) > 0 else "*** ZERO TRADES ***"
        print(f"MC totaltrades   : {tt_last}   <-- MC's own count (the authoritative answer)  [{verdict}]")
    if i_np is not None:
        print(f"final netprofit  : {np_last}")
    if i_tag is not None:
        if tags:
            print(f"order tags fired : {dict(tags)}")
        else:
            print("order tags fired : NONE - no entry/exit was ever issued (signal path never fired, or gated off)")
    if i_mp is not None:
        print(f"marketpos seen   : {sorted(mp_values)}   position changes: {mp_changes}   bars in-position: {bars_in_pos:,}")
    if i_cc is not None:
        print(f"max contracts    : {cc_max}")

    print("\n--- DATA2 HEALTH ---")
    if i_d2 is not None:
        total = d2_nonzero + d2_zero_or_blank
        pct = (100.0 * d2_nonzero / total) if total else 0.0
        print(f"d2close populated: {d2_nonzero:,}/{total:,} rows non-zero ({pct:.1f}%)"
              + ("" if pct > 50 else "  <-- mostly zero/blank: Data2 missing or misaligned"))
    else:
        print("d2close column   : not found in header")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python pl_transpiler/tools/analyze_capture.py <capture.csv>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
