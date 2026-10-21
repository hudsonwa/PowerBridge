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
"""Every name in pl_transpiler.__all__ (documented in docs/API.md) imports."""

import importlib
import subprocess
import sys

import pl_transpiler


def test_all_names_import():
    for name in pl_transpiler.__all__:
        assert getattr(pl_transpiler, name) is not None, name


def test_from_import_star_in_fresh_interpreter():
    code = ("from pl_transpiler import *; import pl_transpiler as p; "
            "assert all(n in globals() for n in p.__all__)")
    subprocess.run([sys.executable, '-c', code], check=True)


def test_catalog_is_the_submodule():
    assert pl_transpiler.catalog is importlib.import_module('pl_transpiler.catalog')
