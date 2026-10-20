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

"""Bar CSV date/time parsing: accepted forms, validation and --date-format."""
import os
import subprocess
import sys

import pytest

from pl_transpiler.engine_dates import _parse_el_date, _parse_el_time
from pl_transpiler.tools.pl_run import load_bars_csv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EL = os.path.join(REPO, "examples", "ma_cross.el")
BARS = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
HEADER = "date,time,open,high,low,close,volume\n"


@pytest.mark.parametrize("text,fmt,want", [
    ("1240102", None, 1240102),
    ("991231", None, 991231),
    ("02/01/2024", None, 1240102),
    ("2/1/2024", None, 1240102),
    ("02-01-2024", None, 1240102),
    ("2024-01-02", None, 1240102),
    ("2024/01/02", None, 1240102),
    ("29/02/2024", None, 1240229),
    ("01/02/2024", "mdy", 1240102),
    ("01/13/2024", "mdy", 1240113),
    ("2024/01/02", "ymd", 1240102),
    ("02/01/2024", "dmy", 1240102),
])
def test_date_forms(text, fmt, want):
    assert _parse_el_date(text, fmt) == want


@pytest.mark.parametrize("text,fmt,needle", [
    ("01/13/2024", None, "--date-format mdy"),
    ("13/13/2024", None, "month 13"),
    ("31/04/2024", None, "day 31"),
    ("29/02/2023", None, "day 29"),
    ("00/01/2024", None, "day 0"),
    ("1241301", None, "month 13"),
    ("1240230", None, "day 30"),
    ("13/01/2024", "mdy", "month 13"),
    ("2024/13/01", "ymd", "month 13"),
    ("x/01/2024", None, "bad date"),
    ("Jan 2 2024", None, "bad date"),
])
def test_bad_dates(text, fmt, needle):
    with pytest.raises(ValueError) as exc:
        _parse_el_date(text, fmt)
    assert needle in str(exc.value) and repr(text) in str(exc.value)


def test_explicit_dmy_has_no_mdy_hint():
    with pytest.raises(ValueError) as exc:
        _parse_el_date("01/13/2024", "dmy")
    assert "--date-format" not in str(exc.value)


@pytest.mark.parametrize("text,want", [
    ("930", 930), ("0", 0), ("2359", 2359), ("09:30", 930),
    ("09:30:00", 930), ("9:05:00", 905), ("23:59:00", 2359),
])
def test_time_forms(text, want):
    assert _parse_el_time(text) == want


@pytest.mark.parametrize("text,needle", [
    ("09:31:05", "seconds"), ("2400", "00:00-23:59"), ("24:00", "00:00-23:59"),
    ("960", "00:00-23:59"), ("09:60", "minute 60"), ("-5", "00:00-23:59"),
    ("9.30", "bad time"), ("1:2:3:4", "bad time"),
])
def test_bad_times(text, needle):
    with pytest.raises(ValueError) as exc:
        _parse_el_time(text)
    assert needle in str(exc.value)


def _csv(tmp_path, rows, name="bars.csv"):
    p = tmp_path / name
    p.write_text(HEADER + "".join(r + "\n" for r in rows))
    return str(p)


def test_loader_date_format_and_line_number(tmp_path):
    path = _csv(tmp_path, ["01/02/2024,09:30:00,1,2,0,1,5",
                           "01/13/2024,09:31:00,1,2,0,1,5"])
    assert [b["date"] for b in load_bars_csv(path, date_format="mdy")] == [1240102, 1240113]
    with pytest.raises(ValueError, match=r"line 3: bad date '01/13/2024'.*--date-format mdy"):
        load_bars_csv(path)


def test_shipped_sample_unchanged():
    bars = load_bars_csv(BARS)
    assert len(bars) == 140
    assert (bars[0]["date"], bars[0]["time"]) == (1240102, 930)


def _run(tool, *args):
    env = dict(os.environ, PYTHONPATH=REPO)
    return subprocess.run([sys.executable, "-m", "pl_transpiler.tools." + tool] + list(args),
                          capture_output=True, text=True, env=env, cwd=REPO, timeout=60)


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
@pytest.mark.parametrize("row,needle", [
    ("01/13/2024,09:30:00,1,2,0,1,5", "line 2: bad date '01/13/2024'"),
    ("31/04/2024,09:30:00,1,2,0,1,5", "day 31"),
    ("02/01/2024,24:00:00,1,2,0,1,5", "bad time '24:00:00'"),
    ("02/01/2024,09:30:05,1,2,0,1,5", "seconds"),
])
def test_cli_one_line_error(tool, tmp_path, row, needle):
    path = _csv(tmp_path, [row])
    proc = _run(tool, EL, path)
    assert proc.returncode == 2, proc.stderr
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith(tool + ": error: bad bars file " + path), lines[0]
    assert needle in lines[0], lines[0]


def _mc_copy(tmp_path, order):
    """The shipped sample rewritten with slash dates in the given field order."""
    out = [HEADER.strip()]
    for line in open(BARS).read().splitlines()[1:]:
        d, t, *rest = line.split(",")
        d, t = int(d), int(t)
        y, m, dd = 1900 + d // 10000, d // 100 % 100, d % 100
        ds = {"dmy": "%02d/%02d/%d" % (dd, m, y), "mdy": "%02d/%02d/%d" % (m, dd, y),
              "ymd": "%d/%02d/%02d" % (y, m, dd)}[order]
        out.append(",".join([ds, "%02d:%02d:00" % (t // 100, t % 100)] + rest))
    p = tmp_path / ("mc_%s.csv" % order)
    p.write_text("\n".join(out) + "\n")
    return str(p)


@pytest.mark.parametrize("order", ["dmy", "mdy", "ymd"])
def test_cli_date_format_same_digest(tmp_path, order):
    want = _run("pl_oracle", EL, BARS)
    assert want.returncode == 0, want.stderr
    got = _run("pl_oracle", EL, _mc_copy(tmp_path, order), "--date-format", order)
    assert got.returncode == 0, got.stderr
    assert got.stdout == want.stdout
    run = _run("pl_run", EL, _mc_copy(tmp_path, order), "--date-format", order,
               "-o", str(tmp_path / "out.csv"))
    assert run.returncode == 0, run.stderr


def test_cli_rejects_unknown_date_format():
    proc = _run("pl_oracle", EL, BARS, "--date-format", "ydm")
    assert proc.returncode == 2 and "invalid choice" in proc.stderr
