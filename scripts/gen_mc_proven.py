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

"""gen_mc_proven.py — regenerate pl_transpiler/tools/mc_proven.json.

The file holds only the code tokens and call names (lower-cased identifiers) of the
ground_truth/ scripts listed in mc_ground_check.PROVEN_SCRIPTS, so the installed wheel can
run el_emit's name check without the repo. tests/test_mc_proven_data.py fails if the
committed file differs from what this script produces.

Usage:  python scripts/gen_mc_proven.py [--check]
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler.tools import mc_ground_check as g  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text = g.dump_proven_data(g.build_proven_data(REPO))
    if argv == ['--check']:
        with open(g.PROVEN_DATA, encoding='utf-8') as fh:
            ok = fh.read() == text
        print("mc_proven.json is up to date" if ok else "mc_proven.json is stale: run scripts/gen_mc_proven.py")
        return 0 if ok else 1
    with open(g.PROVEN_DATA, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    print("wrote " + os.path.relpath(g.PROVEN_DATA, REPO))
    return 0


if __name__ == '__main__':
    sys.exit(main())
