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

"""The public tier as pytest tests: `pytest -m public_tier` runs exactly the
members of tests/run_public_tier.py, one test per member, with the same
PASS/SKIP/FAIL rules (capture-dependent members skip without captures; a listed
member whose file is missing fails). The member list lives in run_public_tier.py
only, so the two entry points cannot drift apart.
"""
import os

import pytest

from tests import capture_gate
from tests.run_public_tier import MEMBERS, REPO, _run_preflight, _run_script


@pytest.mark.public_tier
@pytest.mark.parametrize("label,path,extra_args,cap_dep", MEMBERS,
                         ids=[m[0] for m in MEMBERS])
def test_public_tier_member(label, path, extra_args, cap_dep):
    if path is not None:
        assert os.path.exists(os.path.join(REPO, path)), (
            f"{path} is listed but missing from the repository")
    if cap_dep and not capture_gate.captures_present():
        pytest.skip("needs MultiCharts captures (not redistributed); "
                    f"attach your own: {capture_gate.CAPTURES_DOC}")
    ok, detail = _run_preflight() if path is None else _run_script(path, extra_args)
    assert ok, f"{label} failed:\n{detail}"
