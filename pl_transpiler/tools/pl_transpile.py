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

    pl_transpile <in.txt> [-o out.py] [--trace] [--dialect {runnable,mirror}]
                 [--partial]

The output is runnable by default (--dialect runnable): cross-bar state (Vars,
lookback, Inputs) lives in a `_state` dict inside the caller's kwargs.
--dialect mirror emits the clean mirror dialect (read back by el_emit /
py_front), which is not runnable on its own. The names match the library API,
transpile(src, dialect=...). --no-persist is an alias for --dialect mirror and
--persist for --dialect runnable. --trace always emits the runnable dialect.

DEFAULT (strict) mode:
  * Valid source           -> exit 0, write Python (to -o, else stdout).
  * Unimplemented keywords  -> print the complete report (EVERY unimplemented
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
import sys
from collections.abc import Sequence
from typing import Any, Optional, cast

from pl_transpiler import transpile
from pl_transpiler.errors import (
    UnimplementedKeywordError,
    format_error_lines,
    format_labelled_error_lines,
)
from pl_transpiler.tools import _cli


def _print_strict_report(in_path: str, errors: list[dict[str, Any]]) -> None:
    """Complete report to stderr: every unimplemented keyword, name + line."""
    n = len(errors)
    mixed = any(e.get('kind') for e in errors)
    what = "error(s)" if mixed else "unimplemented EL keyword(s)"
    print(f"pl_transpile: cannot transpile {in_path!r}: {n} {what}:",
          file=sys.stderr)
    for txt in (format_labelled_error_lines if mixed else format_error_lines)(errors):
        print(f"  {txt}", file=sys.stderr)
    print("no output written (strict mode). Re-run with --partial to emit "
          "execution-time stubs for incremental porting.", file=sys.stderr)


def _print_partial_manifest(in_path: str, errors: list[dict[str, Any]]) -> None:
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


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pl_transpile",
        description="Forward transpiler: EL/PowerLanguage source -> Python.")
    ap.add_argument("in_file", help="EL/PowerLanguage source file.")
    ap.add_argument("-o", "--output", dest="output", default=None,
                    help="write Python here (default: stdout).")
    ap.add_argument("--trace", action="store_true",
                    help="emit trace-mode Python (per-bar variable capture).")
    ap.add_argument("--dialect", dest="dialect", choices=("runnable", "mirror"),
                    help="output dialect, as in transpile(src, dialect=...): "
                         "'runnable' (default) keeps cross-bar _state and runs "
                         "as a backtest; 'mirror' is the clean dialect without "
                         "_state (input for el_emit; NOT runnable as a "
                         "backtest). --trace always emits 'runnable'.")
    ap.add_argument("--no-persist", dest="dialect", action="store_const",
                    const="mirror",
                    help="alias for --dialect mirror.")
    ap.add_argument("--persist", dest="dialect", action="store_const",
                    const="runnable",
                    help="alias for --dialect runnable (the default).")
    ap.add_argument("--partial", action="store_true",
                    help="opt-in partial mode: unimplemented constructs become "
                         "execution-time stubs under a NOT-FAITHFUL watermark.")
    ap.set_defaults(dialect="runnable")
    _cli.add_version(ap, "pl_transpile")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return _cli.run("pl_transpile", _main, args)


def _main(args: argparse.Namespace) -> int:
    output = _cli.output_path(args.output)
    src = open(args.in_file, encoding="utf-8").read()

    # Detect unimplemented keywords via the strict transpile (collect-all).
    errors: list[dict[str, Any]] = []
    strict_out: Optional[str]
    try:
        dialect = "runnable" if args.trace else args.dialect
        strict_out = transpile(src, trace=args.trace, dialect=dialect)
    except UnimplementedKeywordError as e:
        errors = e.errors
        strict_out = None

    if not args.partial:
        if errors:
            _print_strict_report(args.in_file, errors)
            return 2
        out = cast(str, strict_out)  # set whenever no keyword errors were raised
    else:
        dialect = "runnable" if args.trace else args.dialect
        out = transpile(src, trace=args.trace, partial=True, dialect=dialect)
        if errors:
            _print_partial_manifest(args.in_file, errors)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"[pl_transpile] wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
