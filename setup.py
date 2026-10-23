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

"""Build hook; all metadata lives in pyproject.toml.

pl_transpiler._examples maps to the repo's examples/ folder and ships only the
package-data files listed in pyproject.toml. Its .py files (hand_written.py)
are examples, not package modules, so they are left out of the wheel.
"""

from setuptools import setup
from setuptools.command.build_py import build_py

_DATA_ONLY = "pl_transpiler._examples"


class BuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        if package == _DATA_ONLY:
            return []
        return super().find_package_modules(package, package_dir)


setup(cmdclass={"build_py": BuildPy})
