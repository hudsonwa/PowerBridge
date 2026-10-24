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

"""``native_status()`` and the kernel line in ``--version`` (#173)."""
import argparse
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pl_transpiler  # noqa: E402
from pl_transpiler.runtime import accel  # noqa: E402
from pl_transpiler.tools import _cli  # noqa: E402

HINT = "; install the powerbridge-accel wheel for your platform, see docs/ACCEL.md"


def _install_fake(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "pl_transpiler._accel", fake)
    monkeypatch.setattr(pl_transpiler, "_accel", fake, raising=False)


@pytest.mark.needs_native
def test_available():
    pytest.importorskip("pl_transpiler._accel")
    if not accel.accel_available():
        pytest.skip("installed kernel does not match this core")
    from pl_transpiler._accel import __version__ as kernel
    assert accel.native_status() == (
        True, "native kernel: powerbridge-accel %s (available)" % kernel)


def test_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "pl_transpiler._accel", None)
    monkeypatch.delattr(pl_transpiler, "_accel", raising=False)
    assert accel.native_status() == (
        False, "native kernel: not available (not installed)" + HINT)
    assert not accel.accel_available()


def test_version_mismatch(monkeypatch):
    fake = types.ModuleType("pl_transpiler._accel")
    fake.FILL_LOG_VERSION = 1
    fake.__version__ = "0.3.0"
    _install_fake(monkeypatch, fake)
    ok, line = accel.native_status()
    assert not ok
    assert line == ("native kernel: not available (powerbridge-accel 0.3.0 implements "
                    "fill-log v1; this powerbridge needs fill-log v%d)"
                    % accel.FILL_LOG_VERSION) + HINT


def test_import_failed(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pl_transpiler._accel":
            raise ImportError("dlopen: wrong architecture")
        return real(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "pl_transpiler._accel", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert accel.native_status() == (
        False, "native kernel: not available (import failed: dlopen: wrong architecture)" + HINT)


def test_version_flag_prints_status(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pl_transpiler._accel", None)
    ap = argparse.ArgumentParser(prog="pl_run")
    _cli.add_version(ap, "pl_run")
    with pytest.raises(SystemExit) as exc:
        ap.parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out == "pl_run %s\nnative kernel: not available (not installed)%s\n" % (
        pl_transpiler.__version__, HINT)
