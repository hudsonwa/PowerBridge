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

"""The README install commands and every package manifest name the same version.

Releases are cut from main, so a README that points at an older tag (or a
manifest left behind on a bump) would install something other than what the
docs describe.
"""
import os
import re

import pl_transpiler

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = pl_transpiler.__version__


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _base(version):
    return version.split(".dev")[0]


def test_manifests_agree():
    pat = re.compile(r'^version = "([^"]+)"', re.M)
    for path in (("pyproject.toml",), ("accel", "pyproject.toml"), ("accel", "Cargo.toml")):
        assert pat.search(_read(*path)).group(1) == VERSION, path


def test_readme_install_points_at_current_release():
    readme = _read("README.md")
    tags = set(re.findall(r"PowerBridge(?:/releases/(?:tag|download))?/?@?v(\d+\.\d+\.\d+)", readme))
    tags |= set(re.findall(r"PowerBridge@v(\d+\.\d+\.\d+)", readme))
    assert tags, "README has no versioned install command"
    assert tags == {_base(VERSION)}, tags
    for wheel_version in re.findall(r"powerbridge(?:_accel)?-(\d+\.\d+\.\d+)-", readme):
        assert wheel_version == _base(VERSION)
