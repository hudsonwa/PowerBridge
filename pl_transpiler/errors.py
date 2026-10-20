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


class UnimplementedKeywordError(Exception):
    """Raised at transpile time when the codegen would emit one or more generic
    `pl_<name>` targets that have no implementation in pl_transpiler.builtins.

    Surfacing this at codegen time (with the original EL keyword spelling and
    source line) replaces the old failure mode: a bare runtime NameError on a
    lowercased `pl_x` name with no EL line.

    Diagnostics are collect-all: a single transpile walks the ENTIRE program and
    reports EVERY unimplemented keyword at once. The `errors` attribute is a list
    of ``{'name': str, 'line': int|None}`` dicts, deduplicated by ``(name, line)``
    and sorted by ``(line, name)`` with ``None`` lines last."""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors if errors is not None else []


def format_error_lines(errors):
    """Render collect-all diagnostics (a list of ``{'name','line'}`` dicts, already
    deduped/sorted by the codegen) as ``"'<name>' at line <L>"`` strings — the FL1
    per-keyword line format, reused by the CLI diagnostic reports and the FL2
    partial-mode watermark/manifest so all three stay identical."""
    lines = []
    for e in errors:
        line = e.get('line')
        where = f" at line {line}" if line else ""
        lines.append(f"'{e['name']}'{where}")
    return lines
