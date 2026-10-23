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

"""The native typed trace prints exactly what the Python trace prints.

For every program in ``examples/`` and ``ground_truth/`` the kernel accepts,
plus synthetic programs that mix int/float/bool/str values, inputs, unread
variables and ``Once``, ``str()`` of each native cell must equal ``run_el``'s
cell, with the same columns and rows (full precision and through the display
sink). A program whose trace types depend on the run must be refused, never
guessed. Skips cleanly when the extension is absent.
"""
import glob
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    accel_available,
    run_native_trace,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import (  # noqa: E402
    display_row_sink,
    format_trace_cell,
    load_bars_csv,
    run_el,
)
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

pytestmark = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)

BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
PROGRAMS = sorted(
    glob.glob(os.path.join(REPO, "examples", "*.el"))
    + glob.glob(os.path.join(REPO, "ground_truth", "*.txt"))
)
# Programs the kernel runs today; each must stay trace-identical.
MUST_RUN = {"ma_cross.el", "GT2_strategy_orders_position.txt"}

TYPE_MIX = """
Inputs: Len(3), Mult(1.5), Flag(true), Tag("x");
Vars: C(0), H(0), B(false), D(0), S(""), K(0.5), N(0), M(0);
C = C + 1;
H = Close / 2;
B = C > 2;
D = IntPortion(Close);
K = K + Len;
N = N + Mult;
if C > 5 then S = "late" else S = "early";
M = AbsValue(C - 10);
if Close > Close[1] then Buy next bar at market;
if Close < Close[1] then SellShort next bar at market;
"""

ONCE_AND_STRINGS = """
Vars: Cnt(0), Line(""), Hit(false), Q(0);
Once Cnt = 100;
Cnt = Cnt + 2;
Hit = Close > Average(Close, 5);
Line = NumToStr(Close, 2) + "," + NumToStr(Cnt, 0);
Q = Sign(Close - Close[1]) + MarketPosition;
if Hit then Buy next bar at market else Sell next bar at market;
"""

KEYWORDS = """
Inputs: Len(10), OutPath("C:\\out.csv");
Vars: X(0), Y(0), Z(0), Unread(0);
X = X + BarNumber;
Y = Square(Len) + MaxList(Close, Open);
Z = Z * 1 + CurrentContracts;
Unread = High - Low;
if X > 20 and MarketPosition = 0 then Buy next bar at market;
if BarsSinceEntry > 3 then Sell next bar at market;
"""

# Python's type for these names depends on which branch ran.
AMBIGUOUS = {
    "int or float": """
Vars: C(0);
if Close > Close[1] then C = C + 1.5 else C = C + 1;
""",
    "int or bool": """
Vars: B(false);
if Close > Close[1] then B = true;
""",
}


@pytest.fixture(scope="module")
def bars():
    loaded = load_bars_csv(BARS_CSV)
    return loaded[0] if isinstance(loaded, tuple) else loaded


def assert_trace_parity(source, bars):
    config = get_config("NQ")
    columns, rows, fills = run_native_trace(source, bars, config)
    expected = run_el(source, bars, config)
    native = [[format_trace_cell(cell) for cell in row] for row in rows]
    assert columns == expected["columns"]
    assert len(native) == len(expected["rows"])
    for index, (got, want) in enumerate(zip(native, expected["rows"])):
        assert got == want, f"bar {index + 1}: " + ", ".join(
            f"{c}={g!r} vs {w!r}" for c, g, w in zip(columns, got, want) if g != w
        )
    assert len(fills) == len(expected["trades"])

    assert displayed(columns, native, config.tick) == displayed(
        columns, expected["rows"], config.tick)


def displayed(columns, rows, tick):
    """Rows as the CLI prints them without --full-precision."""
    out = []
    header, row = display_row_sink(out.append, tick)
    header(columns)
    for cells in rows:
        row(cells)
    return out


@pytest.mark.parametrize("path", PROGRAMS, ids=os.path.basename)
def test_every_accepted_program_traces_like_python(path, bars):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    try:
        assert_trace_parity(source, bars)
    except AccelRefused as exc:
        if os.path.basename(path) in MUST_RUN:
            raise
        pytest.skip(f"kernel refuses this program: {exc}")


@pytest.mark.parametrize("source", [TYPE_MIX, ONCE_AND_STRINGS, KEYWORDS],
                         ids=["type_mix", "once_and_strings", "keywords"])
def test_synthetic_programs_trace_like_python(source, bars):
    assert_trace_parity(source, bars)


@pytest.mark.parametrize("kinds", sorted(AMBIGUOUS))
def test_run_dependent_types_are_refused(kinds, bars):
    with pytest.raises(AccelRefused, match=f"depends on the run \\({kinds}\\)"):
        run_native_trace(AMBIGUOUS[kinds], bars, get_config("NQ"))


# --- pl_run CLI: native trace by default, byte-identical CSV -----------------

def _cli(argv, tmp_path, name, capsys):
    from pl_transpiler.tools import pl_run
    out = tmp_path / name
    assert pl_run.main(argv + ["-o", str(out)]) == 0
    return out.read_bytes(), capsys.readouterr().err


@pytest.mark.parametrize("extra", [
    [], ["--full-precision"],
    ["--columns", "MarketPosition,NetProfit,TotalTrades"],
    ["--columns", "Date,Time,Close,CurrentBar,Fast,Slow,FastLen"],
    ["--max-bars-back", "21"],
    ["--max-bars-back", "50", "--columns", "BarNumber,Date,Close,MarketPosition"],
    ["--max-bars-back", "21", "--max-entries-per-direction", "1"],
])
def test_pl_run_cli_native_csv_is_byte_identical(extra, tmp_path, capsys):
    el = os.path.join(REPO, "examples", "ma_cross.el")
    want, err_py = _cli([el, BARS_CSV, "--python"] + extra, tmp_path, "py.csv", capsys)
    got, err_nat = _cli([el, BARS_CSV] + extra, tmp_path, "nat.csv", capsys)
    assert got == want
    assert "[python]" in err_py and "pure-Python" not in err_py
    assert "round-turns [native]" in err_nat and "pure-Python" not in err_nat


def test_pl_run_cli_unsupported_option_falls_back_loudly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("pl_transpiler.runtime.accel._ACCEL_MISSING_WARNED", False)
    el = os.path.join(REPO, "examples", "ma_cross.el")
    want, _ = _cli([el, BARS_CSV, "--python", "--from-bar", "30"],
                   tmp_path, "py.csv", capsys)
    got, err = _cli([el, BARS_CSV, "--native", "--from-bar", "30"],
                    tmp_path, "nat.csv", capsys)
    assert got == want
    assert "using the pure-Python engine" in err and "windowed" in err
    assert "[python]" in err


def test_old_kernel_trace_refuses_max_bars_back(monkeypatch, bars):
    import pl_transpiler._accel as accel
    monkeypatch.delattr(accel, "TRACE_RUN_OPTIONS", raising=False)
    el = open(os.path.join(REPO, "examples", "ma_cross.el"), encoding="utf-8").read()
    with pytest.raises(AccelRefused, match="predates max_bars_back"):
        run_native_trace(el, bars, get_config("NQ"), max_bars_back=21)


def test_pl_run_cli_kernel_refusal_falls_back_loudly(monkeypatch, tmp_path, capsys):
    def refuse(*_a, **_k):
        raise AccelRefused("synthetic refusal")
    monkeypatch.setattr("pl_transpiler.runtime.accel.run_native_trace", refuse)
    el = os.path.join(REPO, "examples", "ma_cross.el")
    want, _ = _cli([el, BARS_CSV, "--python"], tmp_path, "py.csv", capsys)
    got, err = _cli([el, BARS_CSV], tmp_path, "nat.csv", capsys)
    assert got == want
    assert ("[pl_run] native path refused: synthetic refusal; "
            "using the pure-Python engine (slower)") in err
    assert "[python]" in err
