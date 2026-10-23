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

from pl_transpiler import lookback  # noqa: E402
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
    "Value1 = ParabolicSAR(0.02, 0.2, 0, Value2, Value3, Value4);",
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
    err = capsys.readouterr().err
    assert "--max-bars-back" in err
    assert "auto" in err


def test_warmup_note_suggests_auto(capsys):
    assert pl_oracle.main([MA_CROSS, SAMPLE]) == 0
    err = capsys.readouterr().err
    assert "reads up to 21 past bars" in err
    assert "--max-bars-back auto (= 21)" in err


# Multi-argument builtins: the length is taken by position, not the last arg.
@pytest.mark.parametrize("call,expected", [
    ("StdDev(Close, 20, 1)", 20),
    ("StandardDev(Close, 30, 2)", 30),
    ("BollingerBand(Close, 20, 2)", 20),
    ("LinearRegValue(Close, 20, 0)", 20),
    ("LinearRegValue(Close, 10, 5)", 15),
    ("MACDSignal(Close, 12, 26, 9)", 35),
])
def test_length_taken_by_position(call, expected):
    assert infer_max_bars_back(f"Value1 = {call};") == expected


def test_every_windowed_builtin_has_a_length_position():
    known = set(lookback._LENGTH_ARGS) | lookback._NOT_IMPLEMENTED
    missing = lookback._WINDOWED_CALLS - known - {"crossesabove", "crossesbelow"}
    assert not missing


# One call per _LENGTH_ARGS entry, run by the runtime at exactly the inferred
# MaxBarsBack. Finite windows: the first evaluated bar must equal the same bar
# with 60 more bars of history (a short window would differ). Recursive ones
# (no finite window) must be past their warm-up value.
_FINITE = {
    "average": "Average(Close, 7)", "averagefc": "AverageFC(Close, 7)",
    "averagetfc": "AverageTFC(Close, 7)", "waverage": "WAverage(Close, 7)",
    "weightedaverage": "WeightedAverage(Close, 7)",
    "highest": "Highest(High, 7)", "lowest": "Lowest(Low, 7)",
    "highestbar": "HighestBar(High, 7)", "lowestbar": "LowestBar(Low, 7)",
    "summation": "Summation(Close, 7)", "momentum": "Momentum(Close, 7)",
    "roc": "ROC(Close, 7)", "rateofchange": "RateOfChange(Close, 7)",
    "stddev": "StdDev(Close, 7, 1)", "standarddev": "StandardDev(Close, 7, 2)",
"bollingerband": "BollingerBand(Close, 7, 2)",
    "countif": "CountIf(Close > Open, 7)", "correlation": "Correlation(Close, Open, 7)",
    "linearregvalue": "LinearRegValue(Close, 7, 4)",
    "linearregslope": "LinearRegSlope(Close, 7)",
    "linearregangle": "LinearRegAngle(Close, 7)",
    "atr": "ATR(7)", "avgtruerange": "AvgTrueRange(7)", "cci": "CCI(7)",
    "fastk": "FastK(7)", "stochk": "StochK(7)", "fastd": "FastD(7, 4)",
    "slowk": "SlowK(7, 4)", "slowd": "SlowD(7, 4, 3)", "stochd": "StochD(7, 4, 3)",
    "stochastic": "STOCHASTIC", "truerange": "TrueRange",
}
# Recursive: name -> (call, values that mean "still warming up").
_RECURSIVE = {
    "xaverage": ("XAverage(Close, 7)", ()),
    "rsi": ("RSI(Close, 7)", (50.0,)),
    "dmiplus": ("DMIPlus(7)", (0.0,)),
    "dmiminus": ("DMIMinus(7)", (0.0,)),
    "adx": ("ADX(7)", (0.0, 50.0)),
    "adxr": ("ADXR(7)", (0.0, 25.0, 50.0)),
    "macd": ("MACD(Close, 5, 9)", ()), "macdvalue": ("MACDValue(Close, 5, 9)", ()),
    "macdsignal": ("MACDSignal(Close, 5, 9, 4)", ()),
    "macddiff": ("MACDDiff(Close, 5, 9, 4)", ()),
}
_STOCHASTIC = ("Vars: oFK(0), oFD(0), oSK(0), oSD(0);\n"
               "Value1 = Stochastic(High, Low, Close, 7, 4, 3, 1, oFK, oFD, oSK, oSD);\n"
               "Value2 = oSD;\n")
_HISTORY = 60


def _zigzag_bars():
    """Sample bars with seeded noisy prices, so a short window gives a
    different value (the sample's opening run is monotonic)."""
    import random
    rnd = random.Random(5)
    bars = pl_run.load_bars(SAMPLE)
    for bar in bars:
        o = 15000 + rnd.randint(-80, 80) / 4
        c = 15000 + rnd.randint(-80, 80) / 4
        bar.update(open=o, close=c, high=max(o, c) + rnd.randint(1, 20) / 4,
                   low=min(o, c) - rnd.randint(1, 20) / 4)
    return bars


def _value2(src, bars, mbb, use_native=False):
    rows = pl_run.run_el(src, bars, NQ, max_bars_back=mbb, use_native=use_native,
                         trace_columns=["BarNumber", "Value2"])["rows"]
    return [float(r[1]) for r in rows]


def test_every_table_entry_has_a_runtime_test():
    assert set(_FINITE) | set(_RECURSIVE) | {"variance"} == set(lookback._LENGTH_ARGS)


@pytest.mark.parametrize("name", sorted(_FINITE))
def test_finite_window_full_at_inferred(name):
    call = _FINITE[name]
    src = _STOCHASTIC if call == "STOCHASTIC" else f"Value2 = {call};"
    n = infer_max_bars_back(src)
    assert n > 0
    bars = _zigzag_bars()
    full = _value2(src, bars, 0)[_HISTORY + n]
    first = _value2(src, bars[_HISTORY:], n)[0]
    assert first == full
    # The check can fail: with no history the first bar is a short window.
    assert _value2(src, bars[_HISTORY:], 0)[0] != _value2(src, bars, 0)[_HISTORY]


def test_variance_full_at_inferred():
    """EL Variance takes (Price, Length, DataType); the runtime takes the
    first two, so check the runtime function on exactly n + 1 bars."""
    from pl_transpiler.runtime.pl_runtime import pl_variance
    n = infer_max_bars_back("Value1 = Variance(Close, 7, 1);")
    closes = [bar["close"] for bar in _zigzag_bars()]
    assert pl_variance(closes[_HISTORY:_HISTORY + n + 1], 7) == pl_variance(closes[:_HISTORY + n + 1], 7)
    assert pl_variance(closes[:3], 7) == 0.0


@pytest.mark.parametrize("name", sorted(_RECURSIVE))
def test_recursive_past_warmup_at_inferred(name):
    call, warming = _RECURSIVE[name]
    src = f"Value2 = {call};"
    n = infer_max_bars_back(src)
    bars = _zigzag_bars()
    first = _value2(src, bars, n)[0]
    assert first == _value2(src, bars, 0)[n]
    assert first != 0.0 and first not in warming
    if name == "adxr":
        # ADXR averages ADX now and Length - 1 bars ago: both past warm-up.
        adx = _value2("Value2 = ADX(7);", bars, 0)
        assert adx[n - 6] not in (0.0, 50.0)


def test_inferred_runtime_native_matches_python():
    if not accel_available():
        pytest.skip("native kernel not built")
    bars = _zigzag_bars()
    src = "Value2 = LinearRegValue(Close, 7, 4) + ADX(7);"
    n = infer_max_bars_back(src)
    assert n == 14
    assert _value2(src, bars, n, True) == _value2(src, bars, n)


# Builtins codegen handles itself (File, NumToStr, FileAppend, ...) are not
# user functions; only a genuine user function refuses.
@pytest.mark.parametrize("src,expected", [
    ('Print(File("out.csv"), NumToStr(Close, 2));', 0),
    ('Value1 = FileAppend("log.csv", NumToStr(Close[2], 2));', 2),
    ('Value1 = StrLen(LeftStr("abc", 2)) + Close[1];', 1),
    ("Inputs: Len(6); Value1 = Close[Len];", 6),
    ("Inputs: Len(6); Value1 = Average(Close, Len);", 6),
    ("Inputs: T(2); Value1 = LinearRegValue(Close, 10, T);", 12),
    ("Value1 = ADX(7);", 14),
    ("Value1 = ADXR(7);", 20),
    # A variable that reads only its own history (a hand-rolled average).
    ("Vars: A(0); A = A[1] * 0.9 + Close * 0.1;", 1),
    ("Vars: F(0); F = Close - Open; Value1 = Average(F, 5);", 5),
])
def test_infer_more(src, expected):
    assert infer_max_bars_back(src) == expected


@pytest.mark.parametrize("src,message", [
    ("Vars: X(0);\nX = Close[3];\nValue1 = X[2];",
     "the variable X reads past bars and is itself read 2 bars back (X[2]) on line 3"),
    ("Vars: Fast(0);\nFast = Average(Close, 9);\nValue1 = Average(Fast, 5);",
     "the variable Fast reads past bars and Average() reads its past values on line 3"),
    ("\nValue1 = MyIndexingFunc(Close);",
     "it calls the user function MyIndexingFunc() on line 2"),
    ("Value1 = Median(Close, 5);", "Median() is not implemented in PowerBridge on line 1"),
    ("Value1 = LinearRegValue(Close, 10, Value2);",
     "the TgtBar of LinearRegValue() is not a number or input on line 1"),
    ("Value1 = 1;\nValue2 = FastD(3, Value1 * 2);",
     "the length of FastD() is not a number or input on line 2"),
])
def test_refusal_names_construct_and_line(src, message):
    with pytest.raises(LookbackUnknown) as exc:
        infer_max_bars_back(src)
    assert str(exc.value) == message


def test_auto_uses_stddev_length_not_its_data_type(tmp_path, capsys):
    src = ("If MarketPosition = 0 and Close > StdDev(Close, 20, 1) + Close[1] Then\n"
           "    Buy Next Bar At Market;\n")
    path = _write(tmp_path, src)
    assert pl_oracle.main([path, SAMPLE, "--python", "--max-bars-back", "auto"]) == 0
    auto = capsys.readouterr()
    assert "max-bars-back 20 (auto)" in auto.err
    assert pl_oracle.main([path, SAMPLE, "--python", "--max-bars-back", "20"]) == 0
    assert capsys.readouterr().out == auto.out
