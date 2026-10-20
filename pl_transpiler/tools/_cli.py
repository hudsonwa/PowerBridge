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
a malformed bars CSV) print ``<tool>: error: <message>`` to stderr and exit 2
instead of dumping a traceback. Set ``PB_DEBUG=1`` to get the traceback back.
"""
import os
import sys

from pl_transpiler.errors import UnimplementedKeywordError
from pl_transpiler.lexer import PLSyntaxError


class CliError(Exception):
    """A user-facing CLI error; its message is printed as-is."""


def debug_enabled():
    return os.environ.get("PB_DEBUG", "") not in ("", "0")


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


def bars_or_error(load, path):
    """Call ``load(path)``, turning a malformed bars file into a CliError."""
    try:
        return load(path)
    except (ValueError, IndexError, StopIteration) as exc:
        raise CliError("bad bars file %s: %s" % (path, exc)) from exc


def _describe(exc):
    if isinstance(exc, OSError) and exc.filename is not None:
        return "%s: %s" % (exc.strerror or exc, exc.filename)
    return str(exc)


def run(prog, fn, *args):
    """Run ``fn(*args)`` and return its rc, reporting user errors in one line (rc 2)."""
    try:
        return fn(*args)
    except (PLSyntaxError, OSError, CliError, UnimplementedKeywordError) as exc:
        if debug_enabled():
            raise
        print("%s: error: %s" % (prog, _describe(exc)), file=sys.stderr)
        return 2
