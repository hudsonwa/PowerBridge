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

"""el_emit.py — minimal reverse-transpiler CLI (GOAL R5).

    python3 pl_transpiler/tools/el_emit.py <in.py|in.txt> [-o out.txt]

Input kind is detected by extension:
  * `.py`  -> py_to_el  (clean mirror-dialect Python -> EL)
  * else   -> parse + emit_el (EL -> AST -> canonical EL round-trip)

Writes the canonical EL (to -o, or stdout if omitted), then runs
pl_transpiler/tools/mc_ground_check.check on the emitted file. FAIL-CLOSED: exits non-zero
if mc_ground_check reports the emitted EL is not compile-safe. No framework.
"""
import argparse
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from pl_transpiler.reverse import py_to_el, el_roundtrip
from pl_transpiler.tools import mc_ground_check


def main(argv=None):
    ap = argparse.ArgumentParser(description="Emit canonical EL from Python or EL")
    ap.add_argument("input", help="input file (.py => py_to_el; else parse+emit)")
    ap.add_argument("-o", "--output", default=None,
                    help="output EL file (default: stdout)")
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        src = f.read()

    if args.input.lower().endswith(".py"):
        el = py_to_el(src)
    else:
        el = el_roundtrip(src)

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(el)
        emitted_path = args.output
    else:
        sys.stdout.write(el)
        # mc_ground_check needs a real path; write a temp copy for the gate.
        tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        tf.write(el)
        tf.close()
        emitted_path = tf.name

    rc = mc_ground_check.check(emitted_path)
    if not args.output:
        os.unlink(emitted_path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
