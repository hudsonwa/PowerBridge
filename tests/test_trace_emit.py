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
test_trace_emit.py — run_el formats trace cells identically whether it
retains rows or streams them through row_sink.

The default library call still returns list[list[str]]. A row_sink caller
does not retain (unless retain_rows=True). Cell format matches the historical
GT/run_el rule: missing and None -> '0', else str(value).

Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import (  # noqa: E402
    format_trace_cell,
    format_trace_row,
    run_el,
)

NQ = get_config('NQ')

# Cross-bar counter so we know trace=True still persists Variables.
COUNT_EL = (
    "Vars: Count(0);\n"
    "Count = Count + 1;\n"
    "If Count = 3 Then Buy (\"LE\") next bar at market;\n"
)


def _bars(n):
    return [
        {'date': 1240102, 'time': 930 + i, 'open': 1.0, 'high': 1.0,
         'low': 1.0, 'close': 1.0, 'volume': 1.0}
        for i in range(n)
    ]


def test_format_trace_cell_matches_historical():
    """Frozen rule: str(v) if v is not None else '0'."""
    assert format_trace_cell(None) == '0'
    assert format_trace_cell(0) == '0'
    assert format_trace_cell(0.0) == '0.0'
    assert format_trace_cell(False) == 'False'
    assert format_trace_cell('') == ''
    assert format_trace_cell(12) == '12'
    assert format_trace_cell(18000.25) == '18000.25'
    assert format_trace_row(
        {'a': None, 'b': 0, 'c': 1.5},
        ['a', 'b', 'c', 'missing'],
    ) == ['0', '0', '1.5', '0']
    print("  [ok] format_trace_cell / format_trace_row match historical None->'0'")


def test_default_retain_rows_and_n_rows():
    bars = _bars(5)
    out = run_el(COUNT_EL, bars, NQ, trace_columns=['count', '_market_position'])
    assert out['columns'] == ['count', '_market_position']
    assert len(out['rows']) == 5
    assert out['n_rows'] == 5
    # Count survives bar to bar under trace=True.
    assert [row[0] for row in out['rows']] == ['1', '2', '3', '4', '5']
    assert out['trades'], "Count=3 Buy should fill"
    print("  [ok] default retain: 5 rows, count 1..5, n_rows=5")


def test_row_sink_does_not_retain_by_default():
    bars = _bars(5)
    retained = run_el(COUNT_EL, bars, NQ, trace_columns=['count', '_market_position'])
    sunk = []
    headers = []
    streamed = run_el(
        COUNT_EL, bars, NQ,
        trace_columns=['count', '_market_position'],
        row_sink=sunk.append,
        header_sink=headers.append,
    )
    assert streamed['rows'] == []
    assert streamed['n_rows'] == 5
    assert headers == [['count', '_market_position']]
    assert sunk == retained['rows']
    assert streamed['trades'] == retained['trades']
    print("  [ok] row_sink streams identical cells and leaves rows=[]")


def test_retain_rows_true_with_sink():
    bars = _bars(4)
    sunk = []
    out = run_el(
        COUNT_EL, bars, NQ,
        trace_columns=['count'],
        row_sink=sunk.append,
        retain_rows=True,
    )
    assert out['rows'] == sunk
    assert out['n_rows'] == 4
    print("  [ok] retain_rows=True keeps rows and still sinks")


def test_zero_bars_header_sink():
    headers = []
    sunk = []
    out = run_el(
        COUNT_EL, [], NQ,
        trace_columns=['count'],
        header_sink=headers.append,
        row_sink=sunk.append,
    )
    assert out['rows'] == []
    assert out['n_rows'] == 0
    assert out['columns'] == ['count']
    assert headers == [['count']]
    assert sunk == []
    print("  [ok] n=0 still delivers columns via header_sink")


def test_cli_stream_writes_csv():
    """pl_run CLI streams header+rows; cells match a retaining run_el call."""
    import csv
    import tempfile

    from pl_transpiler.tools.pl_run import main as pl_run_main

    bars = _bars(5)
    retained = run_el(COUNT_EL, bars, NQ, trace_columns=['count', '_market_position'])
    with tempfile.TemporaryDirectory() as td:
        el_path = os.path.join(td, 's.el')
        bars_path = os.path.join(td, 'bars.csv')
        out_path = os.path.join(td, 'out.csv')
        with open(el_path, 'w', encoding='utf-8') as f:
            f.write(COUNT_EL)
        with open(bars_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['date', 'time', 'open', 'high', 'low', 'close', 'volume'])
            for b in bars:
                w.writerow([b['date'], b['time'], b['open'], b['high'],
                            b['low'], b['close'], b['volume']])
        rc = pl_run_main([el_path, bars_path, '--columns', 'count,_market_position',
                          '-o', out_path])
        assert rc == 0
        with open(out_path, newline='', encoding='utf-8') as f:
            r = csv.reader(f)
            header = next(r)
            rows = [row for row in r if row]
        assert header == ['count', '_market_position']
        assert rows == retained['rows']
    print("  [ok] CLI stream CSV matches retaining run_el cells")


def main():
    tests = [
        test_format_trace_cell_matches_historical,
        test_default_retain_rows_and_n_rows,
        test_row_sink_does_not_retain_by_default,
        test_retain_rows_true_with_sink,
        test_zero_bars_header_sink,
        test_cli_stream_writes_csv,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL TRACE-EMIT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
