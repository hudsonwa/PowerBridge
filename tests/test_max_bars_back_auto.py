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

"""--max-bars-back auto: resolves to the lookback inferred from the source,
shows it on the status line, matches the explicit value on both paths, and
refuses (rather than guesses) when the lookback cannot be inferred."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.lookback import LookbackUnknown, infer_max_bars_back  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402

NQ = get_config("NQ")
MA_CROSS = os.path.join(REPO, "examples", "ma_cross.el")
SAMPLE = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
BREAKOUT = ("Inputs: Len(10);\n"
            "If MarketPosition = 0 and Close > Highest(High, Len)[1] Then\n"
            "    Buy (\"BO\") Next Bar At Market;\n"
            "If BarsSinceEntry >= 5 Then Sell (\"BX\") Next Bar At Market;\n")
UNKNOWN = "Value1 = Close[Value2];\nIf Value1 > 0 Then Buy Next Bar At Market;\n"


@pytest.mark.parametrize("src,expected", [
    ('If MarketPosition = 0 Then Buy ("LE") next bar at market;', 0),
    ("Value1 = Close[3];", 3),
    ("Value1 = Average(Close, 5);", 5),
    ("Value1 = Highest(High, 10)[1];", 11),
    ("Inputs: Len(7); Value1 = Lowest(Low, Len)[2];", 9),
    ("Value1 = TrueRange;", 1),
    ("Value1 = Average(Close[2], 4);", 6),
    (BREAKOUT, 11),
])
def test_infer(src, expected):
    assert infer_max_bars_back(src) == expected


def test_infer_ma_cross_is_quickstart_value():
    with open(MA_CROSS, encoding="utf-8") as fh:
        assert infer_max_bars_back(fh.read()) == 21


@pytest.mark.parametrize("src", [
    UNKNOWN,
    "Vars: n(3); Value1 = Average(Close, n * 2);",
    "Value1 = Close of 2 bars ago;",
    "Value1 = MyIndexingFunc(Close);",
])
def test_infer_unknown_raises(src):
    with pytest.raises(LookbackUnknown):
        infer_max_bars_back(src)


def _write(tmp_path, src):
    path = tmp_path / "s.el"
    path.write_text(src, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("engine", ["--native", "--python"])
def test_oracle_auto_equals_explicit(engine, capsys):
    if engine == "--native" and not accel_available():
        pytest.skip("native kernel not built")
    assert pl_oracle.main([MA_CROSS, SAMPLE, "--max-bars-back", "21", engine]) == 0
    explicit = capsys.readouterr()
    assert pl_oracle.main([MA_CROSS, SAMPLE, "--max-bars-back", "auto", engine]) == 0
    auto = capsys.readouterr()
    assert auto.out == explicit.out
    status = auto.err.strip().splitlines()[-1]
    assert ", max-bars-back 21 (auto) [" in status
    assert "max-bars-back" not in explicit.err.strip().splitlines()[-1]


def test_oracle_native_and_python_agree_on_auto(capsys):
    outs = []
    for engine in ("--native", "--python"):
        assert pl_oracle.main([MA_CROSS, SAMPLE, "--max-bars-back", "auto", engine]) == 0
        outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1]


@pytest.mark.parametrize("engine", ["--native", "--python"])
def test_run_auto_equals_explicit(engine, tmp_path, capsys):
    if engine == "--native" and not accel_available():
        pytest.skip("native kernel not built")
    a, b = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    assert pl_run.main([MA_CROSS, SAMPLE, "--max-bars-back", "21", engine, "-o", a]) == 0
    capsys.readouterr()
    assert pl_run.main([MA_CROSS, SAMPLE, "--max-bars-back", "auto", engine, "-o", b]) == 0
    err = capsys.readouterr().err
    assert ", max-bars-back 21 (auto) [" in err
    with open(a, encoding="utf-8") as fa, open(b, encoding="utf-8") as fb:
        assert fa.read() == fb.read()


def test_library_accepts_auto():
    bars = pl_run.load_bars(SAMPLE)
    for use_native in (False, True):
        auto = pl_oracle.run_oracle(BREAKOUT, bars, NQ, max_bars_back="auto",
                                    use_native=use_native)
        explicit = pl_oracle.run_oracle(BREAKOUT, bars, NQ, max_bars_back=11,
                                        use_native=use_native)
        assert auto["text"] == explicit["text"]
        assert auto["fills"] == 6
    assert (pl_run.run_el(BREAKOUT, bars, NQ, max_bars_back="auto")["trades"]
            == pl_run.run_el(BREAKOUT, bars, NQ, max_bars_back=11)["trades"])
    with pytest.raises(ValueError, match="cannot infer"):
        pl_oracle.run_oracle(UNKNOWN, bars, NQ, max_bars_back="auto")


@pytest.mark.parametrize("tool", [pl_oracle, pl_run])
def test_unknown_lookback_is_an_error(tool, tmp_path, capsys):
    el = _write(tmp_path, UNKNOWN)
    assert tool.main([el, SAMPLE, "--max-bars-back", "auto"]) == 2
    err = capsys.readouterr().err
    assert "--max-bars-back auto: cannot infer the lookback" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("tool", [pl_oracle, pl_run])
@pytest.mark.parametrize("bad", ["x", "-1", "autox", "1.5"])
def test_bad_values_rejected(tool, bad, capsys):
    with pytest.raises(SystemExit) as exc:
        tool.main([MA_CROSS, SAMPLE, "--max-bars-back", bad])
    assert exc.value.code == 2
    assert "--max-bars-back" in capsys.readouterr().err


def test_warmup_note_suggests_auto(capsys):
    assert pl_oracle.main([MA_CROSS, SAMPLE]) == 0
    err = capsys.readouterr().err
    assert "reads up to 21 past bars" in err
    assert "--max-bars-back auto (= 21)" in err
