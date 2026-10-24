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

"""Transpile-time diagnostic exceptions for the PowerLanguage code generator."""
import re
from typing import Any, Optional


class UnimplementedKeywordError(Exception):
    """Raised at transpile time when the codegen would emit one or more generic
    `pl_<name>` targets that have no implementation in the runtime.

    Surfacing this at codegen time names the original EL keyword spelling and
    source line, instead of letting the run hit a NameError on a lowercased
    `pl_x` name with no EL line.

    Diagnostics are collect-all: a single transpile walks the ENTIRE program and
    reports EVERY unimplemented keyword at once. The `errors` attribute is a list
    of ``{'name': str, 'line': int|None}`` dicts (plus ``'kind': 'undeclared'``
    and its source ``'spelling'`` for an assignment to a variable that was never
    declared), deduplicated by ``(name, line)``
    and sorted by ``(line, name)`` with ``None`` lines last."""

    def __init__(self, message: str, errors: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(message)
        self.errors = errors if errors is not None else []


def format_error_lines(errors: list[dict[str, Any]]) -> list[str]:
    """Render collect-all diagnostics (a list of ``{'name','line'}`` dicts, already
    deduped/sorted by the codegen) as ``"'<name>' at line <L>"`` strings — the
    per-keyword line format, reused by the CLI diagnostic reports and the
    partial-mode watermark/manifest so all three stay identical."""
    return [_describe(e) for e in errors]


def format_labelled_error_lines(errors: list[dict[str, Any]]) -> list[str]:
    """Like :func:`format_error_lines`, but each unimplemented keyword is
    labelled ``"unimplemented EL keyword '<name>' at line <L>"`` so every item
    of a mixed-kind error report says what is wrong on its own."""
    return [_describe(e) if e.get('kind') else "unimplemented EL keyword " + _describe(e)
            for e in errors]


def _describe(e: dict[str, Any]) -> str:
    line = e.get('line')
    where = f" at line {line}" if line else ""
    if e.get('kind') == 'undeclared':
        name = e.get('spelling', e['name'])
        return f"undeclared variable '{name}'{where} (declare it under Vars:)"
    if e.get('kind') == 'arity':
        name = e.get('spelling', e['name'])
        return f"'{name}'{where} {e['detail']}"
    if e.get('kind') == 'unsupported':
        name = e.get('spelling', e['name'])
        return f"'{name}'{where}: {e['detail']}"
    return f"'{e['name']}'{where}"


def unimplemented_message(errors: list[dict[str, Any]]) -> str:
    """The one-line UnimplementedKeywordError message for ``errors``. A single
    unimplemented keyword uses the short one-line format."""
    if len(errors) == 1:
        e = errors[0]
        if e.get('kind') in ('undeclared', 'arity', 'unsupported'):
            return _describe(e)
        where = f" at line {e['line']}" if e['line'] else ""
        return f"unimplemented EL keyword '{e['name']}'{where}"
    if any(e.get('kind') in ('undeclared', 'arity', 'unsupported') for e in errors):
        return (f"cannot transpile: {len(errors)} errors: "
                + "; ".join(format_labelled_error_lines(errors)))
    return (f"cannot transpile: {len(errors)} unimplemented keywords: "
            + "; ".join(_describe(e) for e in errors))


def restore_spelling(errors: list[dict[str, Any]], source: str) -> None:
    """Record each undeclared variable's spelling in ``source`` under the
    ``'spelling'`` key (the lexer lowercases ``'name'``). Mutates ``errors``."""
    lines = source.splitlines()
    for e in errors:
        line = e.get('line')
        if (e.get('kind') not in ('undeclared', 'arity', 'unsupported')
                or not line or line > len(lines)):
            continue
        m = re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(e['name']),
                      lines[line - 1], re.IGNORECASE)
        if m:
            e['spelling'] = m.group(0)
