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

from .lexer import PLSyntaxError, _source_line
from .parser_tables import (
    ORDER_ACTIONS, RISK_FUNCS, PLOT_NAMES, DATA_SERIES, COLOR_CONSTANTS,
    STYLE_CONSTANTS, PERIOD_FUNCS, BUILTIN_ARITY, BUILTIN_FUNC_MAP,
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


def _collect_declared(ast):
    """Names introduced by Input/Variable/Array declarations and for-loop vars."""
    declared = set()

    def walk(n):
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


def _is_known_name(name, declared):
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


def semantic_check(ast, source='', strict_undefined=False):
    """Post-parse semantic validation.

    Catches, with precise line:col locations:
      (a) wrong arity for KNOWN builtins (BUILTIN_ARITY), and
      (b) undefined identifiers — used but not declared and not a known builtin.

    Arity errors are always raised (high confidence). Undefined-identifier
    findings are returned as a list of warning strings by default; pass
    strict_undefined=True to raise on the first one instead. This keeps valid
    code untouched while still surfacing typos for the LLM to self-correct.
    """
    declared = _collect_declared(ast)
    warnings = []

    def err(message, node, hint=''):
        line = node.get('_line', 0)
        col = node.get('_col', 0)
        raise PLSyntaxError(message, line, col, _source_line(source, line), hint)

    def walk(n):
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
                if not _is_known_name(name, declared):
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
