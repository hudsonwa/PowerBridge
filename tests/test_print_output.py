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

"""``Print`` output: captured by run_el / run_oracle, shown by the CLIs.

The native kernel must print exactly the lines the Python engine prints, and
printing must never change the fills, the digest or stdout.
"""

import glob
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.oracle import run_oracle  # noqa: E402
from pl_transpiler.run import load_bars_csv, run_el  # noqa: E402
from pl_transpiler.runtime import accel  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
NQ = get_config("NQ")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")
PROGRAMS = sorted(
    glob.glob(os.path.join(REPO, "examples", "*.el"))
    + glob.glob(os.path.join(REPO, "ground_truth", "GT2*.txt"))
)

PRINTS = """
Inputs: OutPath("unused.txt");
Vars: X(0), Tag("t"), Up(false);
X = X + 1;
Up = Close > Open;
Once Print(File(OutPath), "header");
If CurrentBar <= 3 then
    Print("bar ", CurrentBar, " close=", Close, " third=", Close / 3,
          " up=", Up, " ", Tag, " neg=", -(Close - Close));
If X = 2 then Buy next bar at market;
If X = 5 then Sell next bar at market;
"""
SILENT = PRINTS.replace("Once Print", "{Once Print").replace(
    "Close));", "Close));}")

needs_accel = pytest.mark.skipif(not accel_available(), reason=ACCEL_SKIP)


@pytest.fixture(scope="module")
def bars():
    return load_bars_csv(BARS_CSV)


def test_python_lines_and_format(bars):
    out = run_el(PRINTS, bars, NQ, max_bars_back=0)
    assert out["prints"][0] == "header"
    assert out["prints"][1] == (
        "bar 1.00 close=15000.00 third=5000.00 up=TRUE t neg=-0.00")
    assert len(out["prints"]) == 4
    assert run_oracle(PRINTS, bars, NQ, max_bars_back=0)["prints"] == out["prints"]


def test_printing_does_not_change_the_trades(bars):
    printed = run_oracle(PRINTS, bars, NQ, max_bars_back=0)
    silent = run_oracle(SILENT, bars, NQ, max_bars_back=0)
    assert silent["prints"] == []
    assert printed["text"] == silent["text"]


@needs_accel
@pytest.mark.needs_native
@pytest.mark.parametrize("max_bars_back", [None, 0])
@pytest.mark.parametrize("path", PROGRAMS + ["<PRINTS>"], ids=os.path.basename)
def test_native_prints_equal_python(bars, path, max_bars_back):
    src = PRINTS if path == "<PRINTS>" else open(path, encoding="utf-8").read()
    native = run_oracle(src, bars, NQ, use_native=True, max_bars_back=max_bars_back)
    python = run_oracle(src, bars, NQ, max_bars_back=max_bars_back)
    assert native["text"] == python["text"]
    assert native["prints"] == python["prints"]
    if path in (GT2, "<PRINTS>"):
        assert native["native"] is True
        assert native["prints"]
    trace = run_el(src, bars, NQ, use_native=True, max_bars_back=max_bars_back)
    assert trace["prints"] == python["prints"]


@needs_accel
@pytest.mark.needs_native
def test_old_kernel_is_refused_for_a_printing_program(bars, monkeypatch, capsys):
    import pl_transpiler._accel as kernel
    monkeypatch.delattr(kernel, "run_program_prints")
    out = run_oracle(PRINTS, bars, NQ, use_native=True, max_bars_back=0)
    assert out["native"] is False
    assert "predates Print output" in out["native_fallback_reason"]
    assert out["prints"] == run_oracle(PRINTS, bars, NQ, max_bars_back=0)["prints"]
    # A program without Print still runs natively on that kernel.
    assert run_oracle(SILENT, bars, NQ, use_native=True, max_bars_back=0)["native"]
    capsys.readouterr()


@pytest.mark.parametrize("engine", ["--python", None])
def test_cli_prints_to_stderr_or_file(engine, tmp_path, capsys):
    if engine is None and not accel_available():
        pytest.skip(ACCEL_SKIP)
    el = tmp_path / "p.el"
    el.write_text(PRINTS, encoding="utf-8")
    flags = ["--max-bars-back", "0"] + ([engine] if engine else [])
    silent = tmp_path / "s.el"
    silent.write_text(SILENT, encoding="utf-8")

    assert pl_oracle.main([str(silent), BARS_CSV, *flags]) == 0
    digest = capsys.readouterr().out
    assert pl_oracle.main([str(el), BARS_CSV, *flags]) == 0
    cap = capsys.readouterr()
    assert cap.out == digest
    assert "header\nbar 1.00 close=15000.00" in cap.err

    out_file = tmp_path / "print.txt"
    assert pl_oracle.main([str(el), BARS_CSV, *flags, "--print", str(out_file)]) == 0
    cap = capsys.readouterr()
    assert cap.out == digest and "header" not in cap.err
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "header" and len(lines) == 4

    assert pl_run.main([str(silent), BARS_CSV, *flags]) == 0
    csv_silent = capsys.readouterr().out
    assert pl_run.main([str(el), BARS_CSV, *flags, "--print", str(out_file)]) == 0
    cap = capsys.readouterr()
    assert out_file.read_text(encoding="utf-8").splitlines() == lines
    assert "header" not in cap.err
    # Same rows apart from the columns of the extra variables.
    assert len(cap.out.splitlines()) == len(csv_silent.splitlines())


def test_refusal_note_is_one_line_with_details_under_verbose():
    reason = "native path refused: unsupported native construct: bar_ref series"
    note = accel.refusal_note("pl_run", reason)
    assert "\n" not in note
    assert note.startswith("[pl_run] note: ran on the Python engine because the "
                           "native kernel does not support bar_ref series yet")
    assert "native path refused" not in note
    assert accel.refusal_detail("pl_run", reason).startswith(f"[pl_run] {reason}")
