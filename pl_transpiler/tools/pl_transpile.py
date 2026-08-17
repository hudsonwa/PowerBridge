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

"""pl_transpile.py — forward transpiler CLI (EL/PowerLanguage -> Python).

    pl_transpile <in.txt> [-o out.py] [--trace] [--partial]

DEFAULT (strict) mode:
  * Valid source           -> exit 0, write Python (to -o, else stdout).
  * Unimplemented keywords  -> print the FL1 complete report (EVERY unimplemented
                               keyword, name + line) to stderr, write NO output
                               file, exit 2. Nothing plausible-but-wrong is ever
                               emitted.

--partial (opt-in) mode:
  * exit 0, write watermarked Python where every unimplemented construct is an
    execution-time stub that RAISES UnimplementedKeywordError when evaluated, and
    print the stub manifest LOUDLY to stderr (banner + one line per stub + a
    do-not-trust warning). Partial output is NOT FAITHFUL — for incremental
    porting only. No framework.
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import transpile  # noqa: E402
from pl_transpiler.errors import UnimplementedKeywordError, format_error_lines  # noqa: E402


def _print_strict_report(in_path, errors):
    """FL1 complete report to stderr: every unimplemented keyword, name + line."""
    n = len(errors)
    print(f"pl_transpile: cannot transpile {in_path!r}: "
          f"{n} unimplemented EL keyword(s):", file=sys.stderr)
    for txt in format_error_lines(errors):
        print(f"  {txt}", file=sys.stderr)
    print("no output written (strict mode). Re-run with --partial to emit "
          "execution-time stubs for incremental porting.", file=sys.stderr)


def _print_partial_manifest(in_path, errors):
    """Loud partial-mode stub manifest to stderr: banner + per-stub + warning."""
    n = len(errors)
    print("=" * 70, file=sys.stderr)
    print(f"PARTIAL TRANSPILE -- NOT FAITHFUL ({in_path})", file=sys.stderr)
    print(f"{n} unimplemented construct(s) replaced with execution-time stubs:",
          file=sys.stderr)
    for txt in format_error_lines(errors):
        print(f"  {txt}", file=sys.stderr)
    print("Each stub raises UnimplementedKeywordError when evaluated -- "
          "do not trust backtest results.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="pl_transpile",
        description="Forward transpiler: EL/PowerLanguage source -> Python.")
    ap.add_argument("in_file", help="EL/PowerLanguage source file.")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write Python here (default: stdout).")
    ap.add_argument("--trace", action="store_true",
                    help="emit trace-mode Python (per-bar variable capture).")
    ap.add_argument("--partial", action="store_true",
                    help="opt-in partial mode: unimplemented constructs become "
                         "execution-time stubs under a NOT-FAITHFUL watermark.")
    return ap


def main(argv=None):
    ap = _build_parser()
    args = ap.parse_args(argv)

    src = open(args.in_file, encoding="utf-8").read()

    # Detect unimplemented keywords via the strict transpile (collect-all).
    errors = []
    try:
        strict_out = transpile(src, trace=args.trace)
    except UnimplementedKeywordError as e:
        errors = e.errors
        strict_out = None

    if not args.partial:
        if errors:
            _print_strict_report(args.in_file, errors)
            return 2
        out = strict_out
    else:
        out = transpile(src, trace=args.trace, partial=True)
        if errors:
            _print_partial_manifest(args.in_file, errors)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"[pl_transpile] wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
