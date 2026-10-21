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

"""Wheel-load gate for the optional accel extra.

Proves the INSTALLED native module -- not the repo source tree -- actually
loads and exposes the current kernel contract:

    pl_transpiler._accel.ping() == 42
    pl_transpiler._accel.run_program is callable (strategy-bearing entry point)
    pl_transpiler._accel.run(...) (bars only, no program) refuses with
        NotImplementedError("ACCEL_FALLBACK_REQUIRED: ...")

The probe runs in a fresh subprocess from a NON-repo cwd so the repo's source
``pl_transpiler`` package cannot shadow the installed ``_accel*.so`` (the exact
shadowing trap documented in accel/README.md). When the extra is absent the
probe's subprocess raises ModuleNotFoundError and this test skips -- the core
tier must stay green with or without the extra.
"""

import subprocess
import sys
import tempfile

import pytest

from tests.skip_messages import ACCEL_SKIP

PROBE = (
    "import pl_transpiler._accel as accel\n"
    "assert accel.__name__ == 'pl_transpiler._accel'\n"
    "assert accel.ping() == 42\n"
    "assert callable(accel.run_program)\n"
    "try:\n"
    "    accel.run([], [], [], [], [], [], [])\n"
    "except NotImplementedError as exc:\n"
    "    assert str(exc).startswith('ACCEL_FALLBACK_REQUIRED'), exc\n"
    "else:\n"
    "    raise SystemExit('bars-only run() must refuse: it carries no program')\n"
    "print('ACCEL_LOAD_OK')\n"
)


def test_installed_accel_wheel_loads():
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    missing = (
        "No module named 'pl_transpiler._accel'" in proc.stderr
        or "No module named 'pl_transpiler'" in proc.stderr
    )
    if proc.returncode != 0 and missing:
        pytest.skip(ACCEL_SKIP)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ACCEL_LOAD_OK" in proc.stdout
