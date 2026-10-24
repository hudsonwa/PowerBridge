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

"""``pl_transpile --dialect`` uses the library's dialect names.

``--dialect {runnable,mirror}`` matches ``transpile(src, dialect=...)``;
``--no-persist`` / ``--persist`` stay as aliases with byte-identical output, and
``--trace`` always emits the runnable dialect.
"""
import glob
import os

import pytest

from pl_transpiler import transpile
from pl_transpiler.tools import pl_transpile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = sorted(glob.glob(os.path.join(REPO, "examples", "*.el")))


def _cli(el, tmp_path, *flags):
    out = tmp_path / ("out%d.py" % len(list(tmp_path.iterdir())))
    assert pl_transpile.main([el, "-o", str(out), *flags]) in (0, None)
    return out.read_text()


@pytest.mark.parametrize("el", EXAMPLES, ids=os.path.basename)
def test_dialect_flag_matches_api_and_aliases(el, tmp_path):
    src = open(el, encoding="utf-8").read()
    mirror = transpile(src, dialect="mirror")
    runnable = transpile(src, dialect="runnable")
    assert mirror != runnable
    assert _cli(el, tmp_path, "--dialect", "mirror") == mirror
    assert _cli(el, tmp_path, "--no-persist") == mirror
    assert _cli(el, tmp_path, "--dialect", "runnable") == runnable
    assert _cli(el, tmp_path, "--persist") == runnable
    assert _cli(el, tmp_path) == runnable


def test_last_flag_wins_and_trace_forces_runnable(tmp_path):
    el = EXAMPLES[0]
    src = open(el, encoding="utf-8").read()
    assert _cli(el, tmp_path, "--no-persist", "--dialect", "runnable") == transpile(src)
    assert _cli(el, tmp_path, "--dialect", "runnable", "--no-persist") == transpile(src, dialect="mirror")
    traced = transpile(src, trace=True)
    assert _cli(el, tmp_path, "--trace", "--dialect", "mirror") == traced
    assert _cli(el, tmp_path, "--trace", "--no-persist") == traced


def test_dialect_rejects_unknown_name(capsys):
    with pytest.raises(SystemExit) as exc:
        pl_transpile._build_parser().parse_args(["x.el", "--dialect", "persist"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_help_names_dialects():
    text = pl_transpile._build_parser().format_help()
    assert "--dialect {runnable,mirror}" in text
    assert "alias for --dialect mirror" in text
