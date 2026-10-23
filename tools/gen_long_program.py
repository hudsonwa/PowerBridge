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

"""gen_long_program.py — write a long synthetic EL strategy for front-end timing.

The program is valid EasyLanguage that both the native kernel and the Python
path run: a block of Inputs/Vars, then repeated blocks of arithmetic, price
lookbacks, comments and guarded orders. It exists to time the lexer, parser
and codegen on a large source (``pl_run --python long.el bars.csv``).

    python tools/gen_long_program.py --lines 3000 > long.el
"""

import argparse
import sys


def generate(lines):
    """EL source of roughly ``lines`` lines."""
    blocks = max(1, (lines - 4) // 10)
    out = ["{ synthetic long program (tools/gen_long_program.py) }",
           "Inputs: Len(3);",
           "Vars: " + ", ".join(f"v{i}(0)" for i in range(blocks)) + ";",
           ""]
    for i in range(blocks):
        out += [
            f"// block {i}",
            f"v{i} = (Close - Open) * {i % 7 + 1} + High[1] - Low[1];",
            f"if v{i} > {i % 5} then",
            "begin",
            f"    v{i} = v{i} / 2 + Close[Len];",
            "end",
            "else",
            f"    v{i} = v{i} - {i % 3};",
            f"if v{i} > 1000000 then Buy next bar at market;",
            f"if v{i} < -1000000 then Sell next bar at market;",
        ]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lines", type=int, default=3000,
                    help="approximate number of source lines (default: 3000)")
    args = ap.parse_args(argv)
    sys.stdout.write(generate(args.lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
