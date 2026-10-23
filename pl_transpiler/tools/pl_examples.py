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

"""Write the README quickstart's example files into ./examples/.

    pl_examples [DIR]

Copies the bundled strategy (ma_cross.el) and synthetic bars
(NQ_sample_bars.csv) so the README commands work after a wheel install,
without a clone. Existing files are never overwritten.
"""

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from typing import Optional

EXAMPLE_FILES = ("ma_cross.el", "NQ_sample_bars.csv")
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir() -> str:
    """Where the bundled files live: pl_transpiler/_examples/ in a wheel
    install, or the repo's examples/ (their only source, mapped into the
    wheel by pyproject.toml) in a source checkout or editable install."""
    for d in (os.path.join(_PKG_DIR, "_examples"),
              os.path.join(os.path.dirname(_PKG_DIR), "examples")):
        if all(os.path.isfile(os.path.join(d, n)) for n in EXAMPLE_FILES):
            return d
    return os.path.join(_PKG_DIR, "_examples")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pl_examples",
        description="Write the README quickstart's example strategy and synthetic bars "
                    "into DIR (never overwrites existing files).")
    ap.add_argument("dir", nargs="?", default="examples",
                    help="Destination directory (default: examples).")
    args = ap.parse_args(argv)
    src = _data_dir()
    missing = [n for n in EXAMPLE_FILES if not os.path.isfile(os.path.join(src, n))]
    if missing:
        print(f"[pl_examples] error: bundled example file(s) missing from this install: "
              f"{', '.join(missing)} (looked in {src}); reinstall powerbridge",
              file=sys.stderr)
        return 1
    os.makedirs(args.dir, exist_ok=True)
    for name in EXAMPLE_FILES:
        dst = os.path.join(args.dir, name)
        if os.path.exists(dst):
            print(f"[pl_examples] {dst} exists, left unchanged", file=sys.stderr)
            continue
        shutil.copyfile(os.path.join(src, name), dst)
        print(f"[pl_examples] wrote {dst}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
