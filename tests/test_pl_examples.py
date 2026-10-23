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

"""pl_examples: the packaged quickstart files match examples/ and never overwrite."""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler.tools import pl_examples  # noqa: E402


def _read(path):
    with open(path, "rb") as f:
        return f.read()


@pytest.mark.parametrize("name", pl_examples.EXAMPLE_FILES)
def test_packaged_copy_is_byte_identical_to_examples(name):
    assert _read(os.path.join(pl_examples._DATA_DIR, name)) == _read(os.path.join(REPO, "examples", name))


def test_writes_files_into_examples_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pl_examples.main([]) == 0
    for name in pl_examples.EXAMPLE_FILES:
        assert _read(tmp_path / "examples" / name) == _read(os.path.join(REPO, "examples", name))


def test_never_overwrites(tmp_path, capsys):
    (tmp_path / "ma_cross.el").write_text("mine")
    assert pl_examples.main([str(tmp_path)]) == 0
    assert (tmp_path / "ma_cross.el").read_text() == "mine"
    assert (tmp_path / "NQ_sample_bars.csv").exists()
    assert "exists, left unchanged" in capsys.readouterr().err


def test_package_data_declared():
    with open(os.path.join(REPO, "pyproject.toml")) as f:
        text = f.read()
    assert '"examples/*.el", "examples/*.csv"' in text
    assert 'pl_examples = "pl_transpiler.tools.pl_examples:main"' in text
