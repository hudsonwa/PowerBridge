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
"""test_negative_array_index.py — a negative array index stops the run with
the same error as an index past the end, on the Python path and the native
kernel, instead of wrapping round to count from the end."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import ELRuntimeError  # noqa: E402
from pl_transpiler.runtime.accel import accel_available  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

BARS_CSV = os.path.join(REPO, "examples", "NQ_sample_bars.csv")

NEGATIVE_READ = "Arrays: a[3](0);\nValue1 = a[-1];\n"
NEGATIVE_WRITE = "Arrays: a[3](0);\na[-1] = Close;\n"
NEGATIVE_COMPUTED = (
    "Arrays: a[3](0);\n"
    "Vars: k(2);\n"
    "Value1 = a[k - 3];\n"
)
PAST_THE_END = "Arrays: a[3](0);\nValue1 = a[4];\n"
IN_RANGE = "Arrays: a[3](0);\nVars: k(1);\na[3] = Close;\nValue1 = a[k - 1] + a[3];\n"

CASES = [
    (NEGATIVE_READ, "array index -1 out of range for 'a' (max index 3) at line 2"),
    (NEGATIVE_WRITE, "array index -1 out of range for 'a' (max index 3) at line 2"),
    (NEGATIVE_COMPUTED, "array index -1 out of range for 'a' (max index 3) at line 3"),
    (PAST_THE_END, "array index 4 out of range for 'a' (max index 3) at line 2"),
]

native = pytest.mark.skipif(not accel_available(),
                            reason="native kernel (pl_transpiler._accel) not built")
PATHS = ["--python", pytest.param("--native", marks=native)]


@pytest.mark.parametrize("tool", [pl_run, pl_oracle])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("src, message", CASES)
def test_cli_stops(tool, path, src, message, tmp_path, capsys):
    el = tmp_path / "s.el"
    el.write_text(src, encoding="utf-8")
    assert tool.main([path, str(el), BARS_CSV]) == 2
    err = [ln for ln in capsys.readouterr().err.strip().splitlines()
           if not ln.startswith("note: ")]
    prog = "pl_run" if tool is pl_run else "pl_oracle"
    assert err == [f"{prog}: error: {message}"]


@pytest.mark.parametrize("use_native", [False, pytest.param(True, marks=native)])
@pytest.mark.parametrize("src, message", CASES)
def test_library_raises_index_error(use_native, src, message):
    with pytest.raises(ELRuntimeError) as info:
        pl_oracle.run_oracle(src, load_bars_csv(BARS_CSV), get_config("NQ"),
                             use_native=use_native)
    assert str(info.value) == message
    assert isinstance(info.value, IndexError)


@pytest.mark.parametrize("use_native", [False, pytest.param(True, marks=native)])
def test_in_range_indexes_still_run(use_native):
    d = pl_oracle.run_oracle(IN_RANGE, load_bars_csv(BARS_CSV), get_config("NQ"),
                             use_native=use_native)
    assert d["fills"] == 0
