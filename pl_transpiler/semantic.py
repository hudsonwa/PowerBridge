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

"""Post-parse semantic validation for the PowerLanguage AST."""

from collections.abc import Set
from typing import Any, NoReturn

from ._types import Node
from .lexer import PLSyntaxError, source_line_at
from .parser_tables import (
    BUILTIN_ARITY,
    BUILTIN_FUNC_MAP,
    COLOR_CONSTANTS,
    DATA_SERIES,
    ORDER_ACTIONS,
    PERIOD_FUNCS,
    PLOT_NAMES,
    RISK_FUNCS,
    STYLE_CONSTANTS,
)

# ---------------------------------------------------------------------------
# Semantic validation (post-parse)
# ---------------------------------------------------------------------------

# Bare data-series / reserved identifiers that codegen resolves implicitly
# (CodeGen._SERIES_PARAMS and _BARE_COMPUTED) and which therefore must NOT be
# reported as undefined by the semantic pass.
_SERIES_PARAMS = {'open', 'high', 'low', 'close', 'volume', 'date', 'time'}
_BARE_COMPUTED = {
    'truerange', 'medianprice', 'range', 'avgprice', 'truehigh', 'truelow',
    'typicalprice', 'weightedclose', 'currentdate', 'currenttime',
    'ticks', 'upticks', 'downticks', 'openint',
}

# PL auto-declared scratch variables Value0..Value99 / Condition0..Condition99,
# plus assorted reserved words that survive parsing as bare idents.
_SCRATCH_VARS = ({f'value{i}' for i in range(100)} |
                 {f'condition{i}' for i in range(100)})
_RESERVED_IDENTS = _SCRATCH_VARS | {
    # Runtime internals emitted by the generated wrapper's prologue
    # (codegen header) — these can resurface as bare idents in round-tripped EL
    # (e.g. the reverse emitter renders a Commentary statement as
    # `_commentary = <text>`). They are legitimate known names, NOT unimplemented
    # keywords, so the write-only lvalue guard must never flag a write to one.
    '_market_position', 'marketposition', '_commentary',
    'all', 'newline', 'pi',
    'lastbaronchart_s', 'lastbaronchart',
    # order-quantity / timing words that may surface as bare idents
    'next', 'this', 'bar', 'at', 'market', 'on', 'shares', 'contracts',
    'share', 'contract', 'total', 'stop', 'limit',
}


def collect_declared(ast: Any) -> set[str]:
    """Names introduced by Input/Variable/Array declarations and for-loop vars."""
    declared: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            t = n.get('type')
            if t in ('var_decl', 'input_decl', 'array_decl'):
                for d in n.get('decls', []):
                    declared.add(d['name'])
            elif t == 'for':
                declared.add(n['var'])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(ast)
    return declared


def add_scratch_decls(ast: Node) -> Node:
    """Declare the undeclared `Value0..99` / `Condition0..99` the program uses.

    EL predeclares them: numeric (`ValueN`) or boolean (`ConditionN`) variables
    that start at 0 / false and keep their value from bar to bar, exactly as if
    the program had `Vars: ValueN(0), ConditionN(false);`. Returns `ast` itself
    when nothing is missing, else a shallow copy whose body starts with that
    `var_decl` (names sorted). A for-loop counter still needs the declaration.
    """
    declared: set[str] = set()
    used: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            t = n.get('type')
            if t in ('var_decl', 'input_decl', 'array_decl'):
                declared.update(d['name'] for d in n.get('decls', []))
            elif t == 'ident' and n.get('name') in _SCRATCH_VARS:
                used.add(n['name'])
            elif t == 'for' and n.get('var') in _SCRATCH_VARS:
                used.add(n['var'])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(ast.get('body', []))
    missing = sorted(used - declared)
    if not missing:
        return ast
    decls: list[dict[str, Any]] = [{'name': name,
                                    'init': ({'type': 'number', 'value': '0'}
                                             if name.startswith('value')
                                             else {'type': 'boolean', 'value': False})}
                                   for name in missing]
    return {**ast, 'body': [{'type': 'var_decl', 'decls': decls}, *ast['body']]}


def is_known_name(name: str, declared: Set[str]) -> bool:
    """True if `name` is a legitimate identifier (declared or a known builtin)."""
    if name in declared:
        return True
    if name in BUILTIN_FUNC_MAP or name in BUILTIN_ARITY:
        return True
    if name in _SERIES_PARAMS or name in DATA_SERIES or name in _BARE_COMPUTED:
        return True
    if name in _RESERVED_IDENTS or name in ORDER_ACTIONS or name in RISK_FUNCS:
        return True
    if name in PLOT_NAMES or name in PERIOD_FUNCS:
        return True
    if name in COLOR_CONSTANTS or name in STYLE_CONSTANTS:
        return True
    return False


def semantic_check(ast: Node, source: str = '', strict_undefined: bool = False) -> list[str]:
    """Post-parse semantic validation.

    Catches, with precise line:col locations:
      (a) wrong arity for KNOWN builtins (BUILTIN_ARITY), and
      (b) undefined identifiers — used but not declared and not a known builtin.

    Arity errors are always raised (high confidence). Undefined-identifier
    findings are returned as a list of warning strings by default; pass
    strict_undefined=True to raise on the first one instead. This keeps valid
    code untouched while still surfacing typos for the LLM to self-correct.
    """
    declared = collect_declared(ast)
    warnings: list[str] = []

    def err(message: str, node: Node, hint: str = '') -> NoReturn:
        line = node.get('_line', 0)
        col = node.get('_col', 0)
        raise PLSyntaxError(message, line, col, source_line_at(source, line), hint)

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            t = n.get('type')
            if t == 'call':
                name = n.get('name', '')
                arity = BUILTIN_ARITY.get(name)
                if arity is not None:
                    nargs = len(n.get('args', []))
                    if nargs not in arity:
                        want = ' or '.join(str(a) for a in sorted(arity))
                        err(f"'{name}' expects {want} argument(s) but got {nargs}",
                            n,
                            hint=f"check the call's arguments — {name} takes {want}")
            elif t == 'ident':
                name = n.get('name', '')
                if not is_known_name(name, declared):
                    line = n.get('_line', 0)
                    col = n.get('_col', 0)
                    msg = (f"undefined identifier '{name}' — not declared as an "
                           f"Input/Variable/Array and not a known builtin")
                    if strict_undefined:
                        err(msg, n, hint="declare it (Variables: %s(0);) or fix the spelling" % name)
                    else:
                        warnings.append(
                            f"line {line}:{col}: {msg}")
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(ast)
    return warnings
