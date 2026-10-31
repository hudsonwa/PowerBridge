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

"""Shared error handling for the console entry points.

User mistakes (an EL syntax error, an unimplemented EL keyword or undeclared
variable, a missing input file, an unknown --config or a malformed --config JSON file,
a malformed bars CSV, Python input outside the mirror dialect, an EL
run-time error such as an array index out of range) print ``<tool>: error: <message>`` to stderr and exit 2
instead of dumping a traceback. Set ``PB_DEBUG=1`` to get the traceback back.
"""
import argparse
import contextlib
import os
import sys
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, Callable, NoReturn, Optional, Union

from pl_transpiler.errors import ELRuntimeError, UnimplementedKeywordError
from pl_transpiler.lexer import PLSyntaxError
from pl_transpiler.py_front import PyFrontError

if TYPE_CHECKING:
    from pl_transpiler.runtime.instrument_config import InstrumentConfig


class CliError(Exception):
    """A user-facing CLI error; its message is printed as-is."""


def debug_enabled() -> bool:
    return os.environ.get("PB_DEBUG", "") not in ("", "0")


class _VersionAction(argparse.Action):
    """``--version``: the tool version, then the native kernel status line."""

    def __init__(self, option_strings: Sequence[str], version: str, dest: str = argparse.SUPPRESS,
                 default: Any = argparse.SUPPRESS,
                 help: str = "show program's version number and exit") -> None:
        super().__init__(option_strings=option_strings, dest=dest, default=default,
                         nargs=0, help=help)
        self.version = version

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: Optional[str] = None) -> NoReturn:
        from pl_transpiler.runtime.accel import native_status
        sys.stdout.write("%s\n%s\n" % (self.version, native_status()[1]))
        parser.exit()


def add_version(ap: argparse.ArgumentParser, prog: str) -> None:
    """Add ``--version`` (prints e.g. ``pl_run 0.13.1`` and the native kernel status)."""
    from pl_transpiler import __version__
    ap.add_argument("--version", action=_VersionAction, version="%s %s" % (prog, __version__))


def output_path(path: Optional[str]) -> Optional[str]:
    """``-o -`` means stdout, same as omitting ``-o``."""
    return None if path == "-" else path


def add_print_arg(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--print", dest="print_file", metavar="FILE", default=None,
                    help="write the strategy's Print lines to FILE instead of "
                         "stderr. The digest / CSV output is unchanged.")


@contextlib.contextmanager
def print_sink(path: Optional[str]) -> Iterator[Callable[[str], Any]]:
    """A sink for Print lines: one line each, to *path* or (None) stderr."""
    if path is None:
        yield lambda line: print(line, file=sys.stderr)
        return
    with open(path, "w", encoding="utf-8") as fh:
        yield lambda line: print(line, file=fh)


def config_or_error(name: str) -> 'InstrumentConfig':
    from pl_transpiler.runtime.instrument_config import ConfigFileError, get_config
    try:
        return get_config(name)
    except KeyError as exc:
        raise CliError(exc.args[0] if exc.args else str(exc)) from exc
    except ConfigFileError as exc:
        raise CliError(str(exc)) from exc


def add_date_format_arg(ap: argparse.ArgumentParser) -> None:
    """The shared ``--date-format`` option for tools that read a bars CSV."""
    ap.add_argument("--date-format", dest="date_format", default=None,
                    choices=("dmy", "mdy", "ymd"),
                    help="field order of slash dates in the bars CSV: dmy "
                         "(D/M/YYYY), mdy (M/D/YYYY) or ymd (YYYY/M/D). Default: "
                         "inferred from the file (a field above 12), an error "
                         "when it cannot tell. EL integer dates (1240102) need "
                         "no option.")


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def _max_bars_back(text: str) -> Union[int, str]:
    if text.strip().lower() == "auto":
        return "auto"
    try:
        return _non_negative_int(text)
    except argparse.ArgumentTypeError as exc:
        raise argparse.ArgumentTypeError(f"{exc} (expected N >= 0 or 'auto')") from None


def add_max_bars_back_arg(ap: argparse.ArgumentParser) -> None:
    """The shared ``--max-bars-back N|auto`` option (MultiCharts MaxBarsBack)."""
    ap.add_argument("--max-bars-back", dest="max_bars_back", default=None,
                    type=_max_bars_back, metavar="N|auto",
                    help="treat the first N bars as history only: no strategy "
                         "evaluation and no orders, and BarNumber starts at 1 on "
                         "bar N+1. 'auto' uses the longest lookback found in the "
                         "strategy source (an error if it cannot be found); 0 "
                         "evaluates every bar. Default: auto, or 0 with a note "
                         "when the lookback cannot be found (MultiCharts "
                         "auto-detects it by default).")


def check_max_bars_back(args: argparse.Namespace, el_source: str) -> str:
    """Resolve ``args.max_bars_back`` in place (``auto`` -> a number).

    Returns the status-line suffix: ``", max-bars-back N (auto)"`` for auto,
    else ``""`` so the line is unchanged. With no ``--max-bars-back`` the
    default is auto, falling back to 0 when the lookback cannot be inferred;
    ``args.max_bars_back_fallback`` records that fallback for the warm-up note."""
    from pl_transpiler.lookback import LookbackUnknown, infer_max_bars_back
    from pl_transpiler.lookback import check_max_bars_back as resolve
    args.max_bars_back_fallback = False
    if args.max_bars_back is None:
        try:
            args.max_bars_back = infer_max_bars_back(el_source)
        except LookbackUnknown:
            args.max_bars_back = 0
            args.max_bars_back_fallback = True
            return ""
        return ", max-bars-back %d (auto)" % args.max_bars_back if args.max_bars_back else ""
    if args.max_bars_back != "auto":
        return ""
    try:
        args.max_bars_back = resolve("auto", el_source)
    except ValueError as exc:
        raise CliError(str(exc).replace("max_bars_back auto", "--max-bars-back auto")) from None
    return ", max-bars-back %d (auto)" % args.max_bars_back


def _positive_int(text: str) -> int:
    value = _non_negative_int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def add_max_entries_per_direction_arg(ap: argparse.ArgumentParser) -> None:
    """The shared ``--max-entries-per-direction N`` option."""
    ap.add_argument("--max-entries-per-direction", dest="max_entries_per_direction",
                    default=None, type=_positive_int, metavar="N",
                    help="refuse a Buy (SellShort) while N entries are already "
                         "open long (short); reversals still fill. Default: "
                         "unlimited, as with MultiCharts' 'allow multiple "
                         "entries' setting. Pass 1 for MultiCharts' documented default.")


def bars_or_error(load: Callable[..., Any], path: str, date_format: Optional[str] = None) -> Any:
    """Call ``load(path)``, turning a malformed or empty bars file into a CliError."""
    try:
        if date_format is not None:
            bars = load(path, date_format=date_format)
        else:
            bars = load(path)
    except (ValueError, IndexError, StopIteration) as exc:
        raise CliError("bad bars file %s: %s" % (path, exc)) from exc
    if len(bars) == 0:
        raise CliError("bars file %s contains no bars" % path)
    return bars


def _describe(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.filename is not None:
        return "%s: %s" % (exc.strerror or exc, exc.filename)
    return str(exc)


def run(prog: str, fn: Callable[..., int], *args: Any) -> int:
    """Run ``fn(*args)`` and return its rc, reporting user errors in one line (rc 2)."""
    try:
        return fn(*args)
    except (PLSyntaxError, PyFrontError, OSError, CliError,
            UnimplementedKeywordError, ELRuntimeError) as exc:
        if debug_enabled():
            raise
        print("%s: error: %s" % (prog, _describe(exc)), file=sys.stderr)
        return 2
