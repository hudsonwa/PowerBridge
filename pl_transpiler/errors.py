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

"""Diagnostic exceptions: transpile-time errors from the PowerLanguage code
generator, and run-time errors raised by the generated strategy code."""
import ast
import re
from types import TracebackType
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


class GeneratedSource(str):
    """Generated Python source (a plain ``str``) that also carries
    ``el_line_map``: generated line number -> EL source line, for the
    statements that have one. Used to name the EL line of a run-time error."""
    el_line_map: dict[int, int] = {}
    el_names: dict[str, str] = {}


class ELRuntimeError(Exception):
    """An EL program failed while it ran (for example an array index out of
    range or a division by zero), as opposed to failing to transpile.

    ``line`` is the EL source line of the statement that failed (``None`` when
    it is not known); ``str(exc)`` is a one-line message such as
    ``array index 4 out of range for 'a' (max index 3) at line 2``. The
    original Python exception is chained as ``__cause__``.

    The raised object is also an instance of the original exception's type
    (``IndexError``, ``ZeroDivisionError``, ...), so ``except IndexError``
    keeps catching it."""

    def __init__(self, message: str, line: Optional[int] = None) -> None:
        Exception.__init__(self, message)
        self.message = message
        self.line = line

    def __str__(self) -> str:  # not the mixed-in base's own formatting
        return self.message


_combined: dict[type, type[ELRuntimeError]] = {}


def _el_error_class(exc_type: type) -> type[ELRuntimeError]:
    """``ELRuntimeError`` mixed with ``exc_type``, or plain ``ELRuntimeError``
    when the two cannot be combined."""
    cls = _combined.get(exc_type)
    if cls is None:
        try:
            cls = type(exc_type.__name__, (ELRuntimeError, exc_type),
                       {'__module__': __name__, '__qualname__': 'ELRuntimeError'})
        except TypeError:
            cls = ELRuntimeError
        _combined[exc_type] = cls
    return cls


# `A[i]` is emitted as `a[-(int(i) + 1)]` (or `a[-(3 + 1)]` for a literal index).
def _el_index(node: ast.expr) -> Optional[ast.expr]:
    if not (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.BinOp) and isinstance(node.operand.op, ast.Add)
            and isinstance(node.operand.right, ast.Constant) and node.operand.right.value == 1):
        return None
    inner = node.operand.left
    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            and inner.func.id == 'int' and len(inner.args) == 1):
        return inner.args[0]
    return inner


def _pure(node: ast.expr) -> bool:
    """True when ``node`` uses only names, constants and arithmetic, so it can
    be evaluated again without side effects."""
    ok = (ast.Expression, ast.Name, ast.Load, ast.Constant, ast.BinOp, ast.UnaryOp,
          ast.operator, ast.unaryop)
    return all(isinstance(n, ok) for n in ast.walk(node))


def _array_bounds(source_line: str, local_vars: dict[str, Any],
                  el_names: dict[str, str]) -> Optional[str]:
    """Name the out-of-range array access on ``source_line``, if it can be found."""
    try:
        tree = ast.parse(source_line.strip())
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)):
            continue
        arr = local_vars.get(node.value.id)
        index = _el_index(node.slice)
        if not isinstance(arr, list) or index is None or not _pure(index):
            continue
        try:
            i = int(eval(compile(ast.Expression(index), '<index>', 'eval'),
                         {'__builtins__': {}}, dict(local_vars)))
        except Exception:  # noqa: BLE001 - not this access
            continue
        if not -len(arr) <= -(i + 1) < len(arr):
            name = el_names.get(node.value.id, node.value.id)
            return f"array index {i} out of range for '{name}' (max index {len(arr) - 1})"
    return None


def _respell(problem: str, el_source: str, line: int) -> str:
    """Use the source spelling of the quoted array name on EL line ``line``."""
    m = re.search(r"'([^']+)'", problem)
    lines = el_source.splitlines()
    if m and line <= len(lines):
        found = re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(m.group(1)),
                          lines[line - 1], re.IGNORECASE)
        if found:
            return problem[:m.start(1)] + found.group(0) + problem[m.end(1):]
    return problem


def el_runtime_error(exc: BaseException, source: str, filename: str,
                     el_source: str = "") -> Optional[ELRuntimeError]:
    """Translate ``exc``, raised while running generated ``source`` compiled as
    ``filename``, into an :class:`ELRuntimeError`. ``el_source`` (optional)
    restores the spelling of an array name. Returns ``None`` when the
    exception did not come from the generated code."""
    tb: Optional[TracebackType] = exc.__traceback__
    frame_tb = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == filename:
            frame_tb = tb
        tb = tb.tb_next
    if frame_tb is None:
        return None
    lineno = frame_tb.tb_lineno
    line = getattr(source, 'el_line_map', {}).get(lineno)
    where = f" at line {line}" if line is not None else ""
    problem = None
    if isinstance(exc, IndexError):
        lines = source.splitlines()
        text = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        problem = _array_bounds(text, frame_tb.tb_frame.f_locals,
                                 getattr(source, 'el_names', {}))
        if problem and line is not None:
            problem = _respell(problem, el_source, line)
        problem = problem or "array index out of range"
    elif isinstance(exc, ZeroDivisionError):
        problem = "division by zero"
    if problem is None:
        problem = (f"internal error{where}: {type(exc).__name__}: {exc} "
                   "(set PB_DEBUG=1 for the full traceback)")
    else:
        problem += where
    try:
        return _el_error_class(type(exc))(problem, line)
    except TypeError:  # a base whose __new__ needs other arguments
        return ELRuntimeError(problem, line)
