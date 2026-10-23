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
"""Indicator calls that used to parse but crash at run time (#165).

Each one now either runs with its EasyLanguage value, checked here against a
hand-computed reference, or stops with a one-line error (exit 2) on both the
native and the Python path."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime._rt_core import f32  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
BARS = load_bars_csv(BARS_CSV)


def _col(name):
    return [b[name] for b in BARS]


H, L, C = _col("high"), _col("low"), _col("close")


def _true_range(i):
    return max(H[i], C[i - 1]) - min(L[i], C[i - 1])


def _variance(i, n, sample):
    w = C[i - n + 1:i + 1]
    mean = sum(w) / n
    var = sum((x - mean) ** 2 for x in w) / n
    return var * n / (n - 1) if sample else var


# (EL expression, first bar index with full history, reference value at bar i)
RUNS = {
    "average-of-expression": ("Average((High + Low) / 2, 10)", 9,
                              lambda i: sum(f32((H[j] + L[j]) / 2)
                                            for j in range(i - 9, i + 1)) / 10),
    "highest-of-lagged": ("Highest(Close[5], 10)", 14,
                          lambda i: max(C[j - 5] for j in range(i - 9, i + 1))),
    "range-2-bars-ago": ("Range[2]", 2, lambda i: H[i - 2] - L[i - 2]),
    "truerange-2-bars-ago": ("TrueRange[2]", 3, lambda i: _true_range(i - 2)),
    "average-of-range": ("Average(Range, 5)", 4,
                         lambda i: sum(H[j] - L[j] for j in range(i - 4, i + 1)) / 5),
    "average-of-expression-lagged": ("Average((High + Low) / 2, 10)[1]", 10,
                                     lambda i: sum(f32((H[j] + L[j]) / 2)
                                                   for j in range(i - 10, i)) / 10),
    "variance-population": ("Variance(Close, 10, 1)", 9,
                            lambda i: _variance(i, 10, False)),
    "variance-sample": ("Variance(Close, 10, 2)", 9,
                        lambda i: _variance(i, 10, True)),
}

REFUSALS = {
    "linearregvalue-negative-tgtbar": (
        "LinearRegValue(Close, 7, -2)",
        "LinearRegValue with a negative TgtBar (-2) is not supported yet"),
    "average-of-variable-expression-in-iff": (
        "IFF(Close > 0, Average(y + 1, 5), 0)",
        "'Average' at line 2: price argument 'y + 1' reads a variable"),
}


def _source(expr):
    return ("Vars: x(0), y(0);\n"
            "x = %s;\n"
            "y = y + 1;\n"
            "If x > x[1] and MarketPosition <= 0 Then Buy Next Bar At Market;\n"
            "If x < x[1] and MarketPosition >= 0 Then SellShort Next Bar At Market;\n"
            % expr)


@pytest.mark.parametrize("case", sorted(RUNS))
def test_value_matches_hand_computed_reference(case):
    expr, first, ref = RUNS[case]
    rows = run_el(_source(expr), BARS, NQ, trace_columns=["x"])["rows"]
    assert len(rows) == len(BARS)
    for i in range(first, len(BARS)):
        assert float(rows[i][0]) == pytest.approx(ref(i), rel=1e-6, abs=1e-6), (case, i)


@pytest.mark.parametrize("max_bars_back", [0, 50])
@pytest.mark.parametrize("case", sorted(RUNS))
def test_native_matches_python(case, max_bars_back):
    src = _source(RUNS[case][0])
    py = run_oracle(src, BARS, NQ, max_bars_back=max_bars_back)
    native = run_oracle(src, BARS, NQ, use_native=True, max_bars_back=max_bars_back)
    assert py["fills"] > 0, py["text"]
    assert native["text"] == py["text"]


def test_lagged_expression_matches_variable_form():
    # Passing an expression must equal assigning it to a variable first.
    direct = _source("Average((High + Low) / 2, 10)")
    via_var = direct.replace("Vars: x(0), y(0);\n", "Vars: x(0), y(0), m(0);\n"
                             "m = (High + Low) / 2;\n").replace(
        "Average((High + Low) / 2, 10)", "Average(m, 10)")
    for mbb in (0, 50):
        assert (run_oracle(direct, BARS, NQ, max_bars_back=mbb)["text"]
                == run_oracle(via_var, BARS, NQ, max_bars_back=mbb)["text"])


@pytest.mark.parametrize("path", ["native", "python"])
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_unsupported_form_stops_with_one_line(case, path, tmp_path, capsys):
    expr, message = REFUSALS[case]
    el = tmp_path / "s.el"
    el.write_text(_source(expr), encoding="utf-8")
    args = [str(el), BARS_CSV] + (["--python"] if path == "python" else [])
    assert pl_oracle.main(args) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert message in err
