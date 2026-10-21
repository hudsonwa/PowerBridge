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
a malformed bars CSV, Python input outside the mirror dialect) print ``<tool>: error: <message>`` to stderr and exit 2
instead of dumping a traceback. Set ``PB_DEBUG=1`` to get the traceback back.
"""
import argparse
import os
import sys

from pl_transpiler.errors import UnimplementedKeywordError
from pl_transpiler.lexer import PLSyntaxError
from pl_transpiler.py_front import PyFrontError


class CliError(Exception):
    """A user-facing CLI error; its message is printed as-is."""


def debug_enabled():
    return os.environ.get("PB_DEBUG", "") not in ("", "0")


def add_version(ap, prog):
    """Add ``--version`` (prints e.g. ``pl_run 0.5.0``)."""
    from pl_transpiler import __version__
    ap.add_argument("--version", action="version", version="%s %s" % (prog, __version__))


def output_path(path):
    """``-o -`` means stdout, same as omitting ``-o``."""
    return None if path == "-" else path


def config_or_error(name):
    from pl_transpiler.runtime.instrument_config import ConfigFileError, get_config
    try:
        return get_config(name)
    except KeyError as exc:
        raise CliError(exc.args[0] if exc.args else str(exc)) from exc
    except ConfigFileError as exc:
        raise CliError(str(exc)) from exc


def add_date_format_arg(ap):
    """The shared ``--date-format`` option for tools that read a bars CSV."""
    ap.add_argument("--date-format", dest="date_format", default=None,
                    choices=("dmy", "mdy", "ymd"),
                    help="field order of slash dates in the bars CSV: dmy "
                         "(D/M/YYYY, the default), mdy (M/D/YYYY) or ymd "
                         "(YYYY/M/D). EL integer dates (1240102) need no option.")


def _non_negative_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def add_max_bars_back_arg(ap):
    """The shared ``--max-bars-back N`` option (MultiCharts MaxBarsBack)."""
    ap.add_argument("--max-bars-back", dest="max_bars_back", default=0,
                    type=_non_negative_int, metavar="N",
                    help="treat the first N bars as history only: no strategy "
                         "evaluation and no orders, and BarNumber starts at 1 on "
                         "bar N+1. MultiCharts skips MaxBarsBack bars (default "
                         "50); pass 50 to reproduce that. Default: 0.")


def _positive_int(text):
    value = _non_negative_int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def add_max_entries_per_direction_arg(ap):
    """The shared ``--max-entries-per-direction N`` option."""
    ap.add_argument("--max-entries-per-direction", dest="max_entries_per_direction",
                    default=None, type=_positive_int, metavar="N",
                    help="refuse a Buy (SellShort) while N entries are already "
                         "open long (short); reversals still fill. Default: "
                         "unlimited, as with MultiCharts' 'allow multiple "
                         "entries' setting. Pass 1 for MultiCharts' documented default.")


def bars_or_error(load, path, date_format=None):
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


def _describe(exc):
    if isinstance(exc, OSError) and exc.filename is not None:
        return "%s: %s" % (exc.strerror or exc, exc.filename)
    return str(exc)


def run(prog, fn, *args):
    """Run ``fn(*args)`` and return its rc, reporting user errors in one line (rc 2)."""
    try:
        return fn(*args)
    except (PLSyntaxError, PyFrontError, OSError, CliError,
            UnimplementedKeywordError) as exc:
        if debug_enabled():
            raise
        print("%s: error: %s" % (prog, _describe(exc)), file=sys.stderr)
        return 2
