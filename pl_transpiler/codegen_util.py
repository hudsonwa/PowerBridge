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

"""Module-level helpers for the PowerLanguage code generator (re-exported by codegen)."""

from typing import Any, Optional

from ._types import Node
from .codegen_tables import _SERIES_PARAMS

_PL_TARGETS_CACHE: Optional[set[str]] = None


def available_pl_targets() -> set[str]:
    """Set of every callable pl_* name in the runtime (the names generated code can
    call). Cached after first use."""
    global _PL_TARGETS_CACHE
    if _PL_TARGETS_CACHE is None:
        from .runtime import pl_runtime as _b
        _PL_TARGETS_CACHE = {
            n for n in dir(_b)
            if n.startswith('pl_') and callable(getattr(_b, n))
        }
    return _PL_TARGETS_CACHE


# Argument counts for builtins the signature catalog does not cover but codegen
# relies on positionally: name -> (min, max).
_EXTRA_ARITY: dict[str, tuple[int, Optional[int]]] = {
    # Stochastic(High, Low, Close, Length, SmoothK, SmoothD, SmoothType,
    #            oFastK, oFastD, oSlowK, oSlowD)
    'stochastic': (11, 11),
}


def call_arity_problem(name: str, nargs: int) -> Optional[str]:
    """Describe why calling EL builtin ``name`` with ``nargs`` arguments is
    malformed, or return None when the count is acceptable or unknown.

    ``_EXTRA_ARITY`` gives exact bounds. For other names, the signature catalog
    only decides the zero-argument case: a keyword the catalog says needs
    arguments, and that is not a zero-arg property, cannot be called bare.
    Positive counts are not checked against the catalog because its
    ``min_arity`` overstates some variadic keywords (MaxList, MinList)."""
    lo_hi: Optional[tuple[int, Optional[int]]] = _EXTRA_ARITY.get(name)
    if lo_hi is None:
        if nargs:
            return None
        from . import catalog
        rec = catalog.get(name)
        if rec is None or rec['zero_arg_property'] or not rec['min_arity']:
            return None
        lo_hi = (rec['min_arity'], None)
    lo, hi = lo_hi
    if nargs >= lo and (hi is None or nargs <= hi):
        return None
    if hi == lo:
        need = '%d argument%s' % (lo, '' if lo == 1 else 's')
    elif hi is None:
        need = 'at least %d argument%s' % (lo, '' if lo == 1 else 's')
    else:
        need = '%d to %d arguments' % (lo, hi)
    return 'takes %s, got %d' % (need, nargs)


_RUNTIME_BARE_CACHE: Optional[set[str]] = None


def _runtime_bound_bare_names() -> set[str]:
    """Bare (lowercased) identifiers that resolve at RUNTIME when emitted verbatim
    into the generated strategy() scope: the function params, the wrapper
    prologue's own locals, every public name pulled in by
    `from pl_transpiler.runtime.pl_runtime import *`, and the Python builtins.
    A bare identifier reaching gen_expr's ident fallback is emitted raw; that is
    only safe when the name is in THIS set (or a generated local). A reserved word
    the parser 'knows' but which binds to nothing here (bare `Pi`, a pl_-only
    keyword, an order-syntax word mis-used as a value) would emit an undefined
    name -> silent NameError, zeroing the trace — so it must fail loud."""
    global _RUNTIME_BARE_CACHE
    if _RUNTIME_BARE_CACHE is None:
        from .runtime import pl_runtime as _rt
        names = {n for n in dir(_rt) if not n.startswith('_')}
        # FAIL-CLOSED: do NOT trust the whole `dir(builtins)` set as a
        # valid runtime binding. A bare EL identifier that happens to collide with a
        # Python builtin name (`all`, `any`, `sum`, `map`, `id`, `next`, `input`,
        # `open`, `list`, ...) is NOT a faithful EL value — emitting it raw silently
        # binds it to the Python builtin (e.g. `Value1 = all;` -> `value1 = all`, the
        # builtin function) and either produces a wrong value or a stray runtime
        # TypeError instead of the contracted fail-loud. Only genuine EL runtime
        # bindings (pl_runtime public names below) + the explicit params/prologue
        # locals count; anything else reaching the bare-ident fallback fails loud.
        names |= {
            # strategy() function params
            'open', 'high', 'low', 'close', 'volume', 'date', 'time',
            # wrapper prologue locals emitted by generate()
            '_orders', '_plots', '_alerts', '_risk', '_commentary', '_drawings',
            '_market_position', '_current_contracts', 'time_s', '_first_bar',
            '_trace', '_state', 'kwargs', '_kw', '_pb_slots',
            # persist/trace hoisted current-bar OHLCV (f32 once per bar)
            '_open', '_high', '_low', '_close', '_volume',
        }
        _RUNTIME_BARE_CACHE = names
    return _RUNTIME_BARE_CACHE

# Current-bar OHLCV names that codegen wraps in f32 for EL single precision.
# Persist/trace hoist these to `_open`/`_high`/… once per bar (see generate()).
_OHLCV_F32_NAMES = ('open', 'high', 'low', 'close', 'volume')
_OHLCV_F32_LOCALS = frozenset('_' + n for n in _OHLCV_F32_NAMES)


def _expr_already_f32(expr: str) -> bool:
    """True if `expr` is a single f32(...) call or a hoisted OHLCV local.

    f32 is idempotent (f32(f32(x)) == f32(x) bit-for-bit), so wrapping again
    is a wasted ctypes round-trip — the `SessionOpenPrice = Open` double wrap.
    `f32(a) + f32(b)` is NOT a single call and must still be wrapped.
    """
    if expr in _OHLCV_F32_LOCALS:
        return True
    if not (expr.startswith('f32(') and expr.endswith(')')):
        return False
    depth = 0
    for i, ch in enumerate(expr):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i == len(expr) - 1
    return False


def _is_numeric_literal_node(node: Any) -> bool:
    """True if `node` is a numeric literal (optional unary minus)."""
    if not isinstance(node, dict):
        return False
    t = node.get('type')
    if t == 'number':
        return True
    if t == 'unaryop' and node.get('op') == '-':
        operand = node.get('operand')
        return isinstance(operand, dict) and operand.get('type') == 'number'
    return False


def _f32_literal_const(node: Node) -> str:
    """Python source of ``f32(<literal>)`` as a folded constant.

    Bit-identical to calling ``pl_runtime.f32`` at run time. persist/trace use
    this for assignments like ``X = 0`` so they are not a ctypes round-trip
    every bar. The clean mirror dialect still emits ``f32(<lit>)``.
    """
    from .runtime.pl_runtime import f32
    if node.get('type') == 'number':
        raw = node['value']
    else:
        raw = '-' + str(node['operand']['value'])
    return repr(f32(float(raw)))


# ---------------------------------------------------------------------------
# Reserved Python names. EL identifiers are emitted as Python names, so a user
# variable called `max`, `str`, `f32` or `pl_average` would rebind a builtin or a
# runtime helper the generated module relies on. Such names are mangled with
# _MANGLE_PREFIX. Every name starting with `_` is reserved too (codegen's own
# locals: `_state`, `_pb_slots`, `_stoch_result`, ...), which also makes the
# mapping one-to-one: an unmangled name never starts with the prefix.
_MANGLE_PREFIX = '_el_'
# Ident names the parser itself produces for EL built-ins (bar data, and
# MarketPosition as `_market_position`). Declaring one cannot be told apart
# from the built-in in the AST, so these are never renamed.
_AST_BUILTIN_IDENTS = frozenset(_SERIES_PARAMS) | {'_market_position'}
_RESERVED_CACHE: Optional[frozenset[str]] = None


def reserved_python_names() -> frozenset[str]:
    """Lowercase names a user identifier must not take in generated code."""
    global _RESERVED_CACHE
    if _RESERVED_CACHE is None:
        import builtins
        import keyword

        from .runtime import pl_runtime as _rt
        names = set(keyword.kwlist) | set(keyword.softkwlist)
        names |= set(dir(builtins)) | set(dir(_rt))
        # Soft keywords and builtins added after Python 3.9, so every supported
        # interpreter renames the same names and emits the same Python.
        names |= {'match', 'case', 'type', 'aiter', 'anext', 'EncodingWarning',
                  'BaseExceptionGroup', 'ExceptionGroup',
                  'PythonFinalizationError'}
        names |= _runtime_bound_bare_names()
        # Bound by generate()'s module prelude and the wrapper itself.
        names |= {'math', 'f32', 'el_round0', 'el_round', 'pl_file',
                  'pl_partial_stub', 'strategy'}
        _RESERVED_CACHE = frozenset(n.lower() for n in names)
    return _RESERVED_CACHE


def mangle_name(name: str) -> str:
    """The Python name codegen emits for EL identifier `name`."""
    if name.startswith('_') or name in reserved_python_names():
        return _MANGLE_PREFIX + name
    return name


def demangle_name(name: str) -> str:
    """Inverse of mangle_name (used when reading generated Python back)."""
    if name.startswith(_MANGLE_PREFIX):
        return name[len(_MANGLE_PREFIX):]
    return name


def mangle_user_names(ast: Node, declared: set[str]) -> tuple[Node, dict[str, str]]:
    """Return (ast, originals): `ast` with every declared name that collides with
    a reserved Python name renamed (idents, declarations, for-loop vars), and a
    {mangled: original} map. Returns `ast` itself when nothing collides."""
    rename = {n: mangle_name(n) for n in declared
              if n not in _AST_BUILTIN_IDENTS and mangle_name(n) != n}
    if not rename:
        return ast, {}

    def walk(n: Any) -> Any:
        if isinstance(n, list):
            return [walk(x) for x in n]
        if not isinstance(n, dict):
            return n
        out = {k: walk(v) for k, v in n.items()}
        t = n.get('type')
        if t == 'ident' and n.get('name') in rename:
            out['name'] = rename[n['name']]
        elif t == 'for' and n.get('var') in rename:
            out['var'] = rename[n['var']]
        elif t in ('var_decl', 'input_decl', 'array_decl'):
            out['decls'] = [{**d, 'name': rename.get(d['name'], d['name'])}
                            for d in out['decls']]
        return out

    return walk(ast), {v: k for k, v in rename.items()}
