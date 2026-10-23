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
"""Series function price arguments that are arithmetic or a nested call (#167).

``Average(Value1 * 2, 5)`` used to repeat the variable's history list and
``Average(Value1 + Value1, 5)`` joined two of them; a nested call crashed.
Each form must now give the same values as assigning the argument to a
variable just before the statement and passing that variable. A call that
does not run exactly once on every bar stops with a one-line error."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

NQ = get_config("NQ")
BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
BARS = load_bars_csv(BARS_CSV)

# (function, price argument, rest of the arguments)
FORMS = {
    "variable-plus-itself": ("Average", "Value1 + Value1", "5"),
    "variable-times-constant": ("Average", "Value1 * 2", "5"),
    "constant-times-variable": ("Average", "2 * y", "5"),
    "xaverage-of-variable-product": ("XAverage", "Value1 * 2", "10"),
    "variable-minus": ("Average", "Value1 - y", "5"),
    "variable-divided": ("Summation", "y / 4", "6"),
    "unary-minus-variable": ("Average", "-y", "5"),
    "close-plus-variable": ("Highest", "Close + Value1", "4"),
    "close-plus-close": ("Average", "Close + Close", "5"),
    "high-plus-low": ("Average", "High + Low", "5"),
    "close-times-constant": ("StandardDev", "Close * 2", "7, 1"),
    "nested-call-on-bar-data": ("Average", "Average(Close - 1, 3)", "5"),
    "nested-call-on-variable": ("Average", "XAverage(y * 3, 4)", "5"),
}


def _sources(func, price, rest):
    """The direct form and the same program with the argument assigned to a
    declared variable just before the statement."""
    head = ("Vars: x(0), y(0), m(0);\n"
            "Value1 = Close / 1000;\n"
            "y = y + Mod(CurrentBar, 3);\n")
    tail = ("If x > x[1] and MarketPosition <= 0 Then Buy Next Bar At Market;\n"
            "If x < x[1] and MarketPosition >= 0 Then SellShort Next Bar At Market;\n")
    direct = head + f"x = {func}({price}, {rest});\n" + tail
    via_var = head + f"m = {price};\nx = {func}(m, {rest});\n" + tail
    return direct, via_var


@pytest.mark.parametrize("case", sorted(FORMS))
def test_values_match_assigning_to_a_variable(case):
    direct, via_var = _sources(*FORMS[case])
    got = run_el(direct, BARS, NQ, trace_columns=["x"])["rows"]
    want = run_el(via_var, BARS, NQ, trace_columns=["x"])["rows"]
    assert len(got) == len(BARS)
    assert got == want


@pytest.mark.parametrize("max_bars_back", [0, 50])
@pytest.mark.parametrize("case", sorted(FORMS))
def test_digest_matches_variable_form_native_and_python(case, max_bars_back):
    direct, via_var = _sources(*FORMS[case])
    py = run_oracle(direct, BARS, NQ, max_bars_back=max_bars_back)
    assert py["fills"] > 0, py["text"]
    assert py["text"] == run_oracle(via_var, BARS, NQ,
                                    max_bars_back=max_bars_back)["text"]
    # The kernel either runs the form with the same result or refuses it
    # (and the Python path runs); it never gives a different answer.
    native = run_oracle(direct, BARS, NQ, use_native=True, max_bars_back=max_bars_back)
    assert native["text"] == py["text"]


def test_lagged_call_is_the_value_bars_ago():
    # Average(m, 5)[2] with a plain variable is a separate case; compare
    # against the unlagged variable form two bars earlier instead.
    direct, via_var = _sources("Average", "Value1 * 2", "5")
    direct = direct.replace("Average(Value1 * 2, 5)", "Average(Value1 * 2, 5)[2]")
    got = run_el(direct, BARS, NQ, trace_columns=["x"])["rows"]
    want = run_el(via_var, BARS, NQ, trace_columns=["x"])["rows"]
    assert got[6:] == want[4:-2]


def test_plain_variable_argument_keeps_its_code():
    from pl_transpiler import transpile
    out = transpile("Vars: x(0);\nValue1 = Close;\nx = Average(Value1, 5);\n")
    assert "__pb_c" not in out and "__pb_a" not in out


# A call that may be skipped or repeated within a bar has no single
# assign-before-the-statement equivalent.
REFUSALS = {
    "inside-if": "If Close > Open Then x = Average(y * 2, 5);",
    "inside-for": "For Value2 = 1 To 2 Begin x = Average(y * 2, 5); End;",
    "right-of-and": "Condition1 = Close > 0 and Average(y * 2, 5) > 0;",
    "inside-iff": "x = IFF(Close > Open, Average(y * 2, 5), 0);",
}


@pytest.mark.parametrize("path", ["native", "python"])
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_conditional_call_stops_with_one_line(case, path, tmp_path, capsys):
    el = tmp_path / "s.el"
    el.write_text("Vars: x(0), y(0);\ny = y + 1;\n" + REFUSALS[case] + "\n"
                  "If x > 0 Then Buy Next Bar At Market;\n", encoding="utf-8")
    args = [str(el), BARS_CSV] + (["--python"] if path == "python" else [])
    assert pl_oracle.main(args) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert ("'Average' at line 3: price argument 'y * 2' reads a variable and "
            "the call does not run exactly once on every bar; assign it to a "
            "declared variable and pass that variable instead") in err
