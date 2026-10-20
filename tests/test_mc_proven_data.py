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

"""The shipped proven-token data (pl_transpiler/tools/mc_proven.json) behind el_emit's name check.

- It must equal what scripts/gen_mc_proven.py produces from ground_truth/, byte for byte.
- If it is missing, el_emit fails closed (one error line, exit 2), never skips the check.
- From an installed wheel, run outside the repo, el_emit gives the README's GT2 result and
  flags the GT1 mirror as NOT-PROVEN (skipped when the wheel cannot be built offline).
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.tools import el_emit, mc_ground_check as g  # noqa: E402

GT1 = os.path.join(REPO, 'ground_truth', 'GT1_functions_indicators.txt')
GT2 = os.path.join(REPO, 'ground_truth', 'GT2_strategy_orders_position.txt')
GT2_EXPECTED = (
    "GT2_from_python.txt: 26 builtins used | 26 PROVEN | 0 NOT-PROVEN | 0 ARITY-BAD | 0 RETURN-TYPE-BAD\n"
    "OK -- every builtin used is known to compile in MultiCharts (names, argument counts, return types).\n")


def _mirror(src_path, dst_path):
    with open(src_path, encoding='utf-8') as fh:
        el = fh.read()
    with open(dst_path, 'w', encoding='utf-8') as fh:
        fh.write(transpile(el, trace=False))


def test_committed_data_matches_ground_truth():
    with open(g.PROVEN_DATA, encoding='utf-8', newline='') as fh:
        committed = fh.read()
    assert committed == g.dump_proven_data(g.build_proven_data(REPO)), \
        "mc_proven.json is stale: run python scripts/gen_mc_proven.py"


def test_missing_data_fails_closed(tmp_path, monkeypatch, capsys):
    mirror = tmp_path / 'GT2_mirror.py'
    _mirror(GT2, str(mirror))
    monkeypatch.setattr(g, 'PROVEN_DATA', str(tmp_path / 'absent.json'))
    monkeypatch.setattr(g, '_proven_cache', None)
    rc = el_emit.main([str(mirror), '-o', str(tmp_path / 'out.txt')])
    err = capsys.readouterr().err.strip().splitlines()
    assert rc == 2
    assert len(err) == 1 and err[0].startswith('el_emit: error: proven-token data'), err


def _can_build():
    # find_spec('build.__main__'), not `import build`: a stray build/ directory
    # (e.g. setuptools output) imports as an empty namespace package.
    import importlib.util
    try:
        if importlib.util.find_spec('build.__main__') is None:
            return False
        import setuptools
    except ImportError:
        return False
    return int(setuptools.__version__.split('.')[0]) >= 77


@pytest.mark.timeout(300)
def test_installed_wheel_outside_repo():
    if not _can_build():
        pytest.skip("needs the 'build' module and setuptools>=77 to build the wheel offline")
    work = tempfile.mkdtemp()
    try:
        dist = os.path.join(work, 'dist')
        subprocess.run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', dist, REPO],
                       check=True, capture_output=True)
        wheel = os.path.join(dist, os.listdir(dist)[0])
        venv = os.path.join(work, 'venv')
        subprocess.run([sys.executable, '-m', 'venv', venv], check=True)
        bindir = os.path.join(venv, 'Scripts' if os.name == 'nt' else 'bin')
        subprocess.run([os.path.join(bindir, 'python'), '-m', 'pip', 'install', '-q', '--no-deps', wheel],
                       check=True, capture_output=True)
        run_dir = os.path.join(work, 'run')
        os.mkdir(run_dir)
        _mirror(GT2, os.path.join(run_dir, 'GT2_mirror.py'))
        _mirror(GT1, os.path.join(run_dir, 'GT1_mirror.py'))
        exe = os.path.join(bindir, 'el_emit')
        p = subprocess.run([exe, 'GT2_mirror.py', '-o', 'GT2_from_python.txt'], cwd=run_dir,
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        assert p.stdout.startswith(GT2_EXPECTED), p.stdout
        p = subprocess.run([exe, 'GT1_mirror.py', '-o', 'GT1_from_python.txt'], cwd=run_dir,
                           capture_output=True, text=True)
        assert p.returncode == 1, p.stdout + p.stderr
        assert 'NOT-PROVEN' in p.stdout and 'atr, roc, stddev' in p.stdout, p.stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)
