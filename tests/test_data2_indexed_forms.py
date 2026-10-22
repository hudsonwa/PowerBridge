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

"""Indexed Data2 spellings fail loudly instead of reading Data1.

``Close of Data2`` is the checked form (g-d2-001-close uses
``C2 = Close of Data2`` and then ``C2[29]``). ``Close[N] of Data2``,
``Close of Data2[N]`` and ``Close Data2`` have not been checked against
MultiCharts, so they stay a parse or unimplemented-keyword error.
"""
import pytest

from pl_transpiler import transpile
from pl_transpiler.errors import UnimplementedKeywordError
from pl_transpiler.lexer import PLSyntaxError
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_run import run_el

_BARS = [
    {"date": 1260102, "time": 1000, "open": 10, "high": 12, "low": 9,
     "close": 11, "volume": 1},
    {"date": 1260102, "time": 1001, "open": 11, "high": 13, "low": 10,
     "close": 12, "volume": 1},
]
_ES = get_config("ES")

# (source, exception, message fragment). The call-argument form and the
# assignment form of Close[29] of Data2 fail at different stages; both
# are loud.
_LOUD = (
    (
        'Vars: id(0);\n'
        'id = TL_New(Date, Time, Close[29] of Data2, Date, Time, Close);\n',
        PLSyntaxError,
        "expected RPAREN but found 'of'",
    ),
    (
        "Vars: A(0);\nA = Close[29] of Data2;\n",
        UnimplementedKeywordError,
        "unimplemented keywords",
    ),
    (
        "Vars: A(0);\nA = Close of Data2[29];\n",
        PLSyntaxError,
        "expected IDENT but found '29'",
    ),
    (
        "Vars: A(0);\nA = Close Data2;\n",
        UnimplementedKeywordError,
        "unimplemented EL keyword 'data2'",
    ),
)


@pytest.mark.parametrize("src,exc,match", _LOUD)
def test_indexed_data2_forms_fail_loud(src, exc, match):
    with pytest.raises(exc, match=match):
        transpile(src)
    with pytest.raises(exc, match=match):
        run_el(src, _BARS, _ES)


def test_close_of_data2_still_reads_the_data2_series():
    """The measured spelling. g-d2-001-close anchors use this, then C2[29]."""
    py = transpile("Vars: A(0);\nA = Close of Data2;\n")
    assert "data2_close" in py
    py_hist = transpile(
        "Vars: C2(0), A(0);\nC2 = Close of Data2;\nA = C2[29];\n")
    assert "data2_close" in py_hist
