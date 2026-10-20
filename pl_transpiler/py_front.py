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

"""py_front.py — Python front-end (GOAL R4): parse the MIRROR DIALECT back to the AST.

The forward transpiler (transpiler/codegen.py) turns the dict AST into clean Python
via `transpile(src, trace=False)`. This module is its inverse over that exact dialect:
it parses the generated Python with the stdlib `ast` module and reconstructs the
parser's dict AST (the shared IR). Composed with `codegen_el.emit_el`, it closes the
loop  EL -> AST1 -> Python -> AST2 -> EL''  (the two-way gate, tests/verify_twoway.py).

It is deliberately NARROW and FAIL-LOUD: any Python construct outside the forward
codegen's own output raises PyFrontError. It never guesses — an unrecognised call,
attribute, statement shape, or expression form is an error, not a plausible default.

THE DIALECT (grounded against transpiler/codegen.py):
  * one `def strategy(open, high, low, close, volume, date, time, **kwargs)`, preceded
    by the fixed import header + `__builtin_len = len`;
  * a fixed prologue (`if not isinstance(close, list): ...`, `_orders=[]`, ... ,
    `_market_position = kwargs.get('market_position', 0)`, `time_s = ...`, optional
    `_first_bar = ...`) — recognised and skipped;
  * Inputs as `Name = kwargs.get('Name', <default>)` (key == target);
  * Vars/Arrays as a contiguous block of pure-init assigns (`Name = f32(<lit>)`, bare
    literal, or `[init] (* (size + 1))?`), each to a NOT-yet-seen name;
  * the executable body: assigns, if / for / while / repeat-until (`while True: ...;
    if C: break`), Once (`if _first_bar: ...; _first_bar = False`), orders
    (`_orders.append((...))`), risk (`_risk[...] = ...`), plots
    (`_plots.setdefault(...).append(...)`), Print/Alert, and a fixed `return {...}`.

DOCUMENTED INVERSIONS (each applied below; the comparator in verify_twoway normalises
the residue that the clean dialect cannot carry):
  * strip `f32(...)` wrappers (codegen.py:815,822,984,1012 — a rounding artifact, not
    AST semantics);
  * `range(int(a), int(b) + 1)` / `range(int(a), int(b) - 1, -1)` -> inclusive for-to /
    for-downto nodes (codegen.py:861-865); the `int(...)` bound wrappers are stripped;
  * collapse the first-bar guard `(<s>[-(n+1)] if __builtin_len(<s>) > 1+n else open[-1])`
    back to a plain `bar_ref` (codegen.py:1575,1581) — an emitted double guard would
    perturb early-bar values;
  * `kwargs.get('Name', d)` (key == target) -> input_decl; the fixed prologue kwargs.get
    calls (market_position / current_contracts / time_s / _first_bar) are recognised and
    skipped, never mistaken for inputs;
  * `_market_position` (Name) -> ident '_market_position'; `__builtin_len` never surfaces
    in the AST (it maps to StrLen or is part of a guard);
  * flat init assigns -> var_decl / array_decl (double-accumulator bare form included);
  * user-variable series refs `(_state['X'] + [X])` -> ident X (a codegen series-context
    shim, codegen.py:1150), and `(_state['X'] + [X])[-(n+1)]` -> bar_ref(X, n);
  * the multi-statement `_stoch_result = pl_stochastic(...); v = f32(_stoch_result[i])`
    fan-out -> a single `stochastic(...)` assign with the output vars appended (codegen.py
    :993-1026);
  * string slices -> LeftStr/RightStr/MidStr, `.lower()/.upper()` -> LowerStr/UpperStr,
    `float(s)` -> StrToNum (codegen.py:1359-1377);
  * the inline `(((int(X)//100)*3600 + (int(X)%100)*60)/86400.0)` -> ELTimeToDateTime,
    `(X // 100)` -> Hour (codegen.py:1383-1433);
  * `pl_pos_trade_field(kwargs, '<name>', a, b)` -> position_kw_call (codegen 2-arg
    PosTrade* form); zero-arg `pl_xxx()` emissions are bare reserved-word properties and
    invert to plain idents; `kwargs.get('current_data_number'/'base_data_number', 1)` ->
    the CurrentDataNumber/BaseDataNumber idents;
  * `pass  # Abort` (a `pass` sharing its block with real statements) -> an `abort` node;
    a lone `pass` is an empty-body filler and contributes nothing;
  * `_line/_col/_warnings` are never produced.

INFORMATION THE CLEAN DIALECT CANNOT CARRY (unrecoverable; the verify_twoway comparator
normalises these, and behaviour is unaffected because the forward emitter already drops
them): IntraBarPersist and the Var `data_ref` (Vars: X(Series, DataN) emits the local
series); `[Attribute = ...]` compile directives; the EntryName/ExitName index and the
MaxPositionProfit-style bars-back index on position/mc keywords; and the many-to-one
BUILTIN_FUNC_MAP call-name aliases (canonicalised through the map).

DIALECT_VERSION: asserted equal to transpiler.codegen.DIALECT_VERSION at import — a cheap
drift tripwire. Bumping the emitted dialect requires touching BOTH files.
"""
import ast
import re

from pl_transpiler.codegen import (
    BUILTIN_FUNC_MAP,
    COMPARE_OPS,
    _SERIES_PARAMS,
    DIALECT_VERSION as _CODEGEN_DIALECT_VERSION,
)
from pl_transpiler.codegen_el import _INV_POSKW, _INV_MCKW

# Must match transpiler.codegen.DIALECT_VERSION (the forward dialect this inverts).
# v3: the forward codegen now emits a same-line trailing `#|` comment (was its own
# line), and py_front re-anchors every `#|` to a statement (was: all dumped to header).
DIALECT_VERSION = 3
assert _CODEGEN_DIALECT_VERSION == DIALECT_VERSION, (
    f"DIALECT_VERSION drift: codegen={_CODEGEN_DIALECT_VERSION} "
    f"py_front={DIALECT_VERSION} — the emitted dialect changed; bump BOTH files.")


class PyFrontError(Exception):
    """Raised on ANY Python construct outside the forward codegen's mirror dialect.
    Fail-loud: py_front never guesses at a plausible AST."""


# Dialect-mismatch hint appended to messages whose actual meaning is "this Python
# construct is outside the mirror dialect py_front inverts".
_DIALECT_HINT = " (construct outside the mirror dialect?)"


def _ln(node):
    """`" at line N"` when the Python AST `node` carries a source line, else ``""``.
    Never fabricates a line — best-effort position info for error messages."""
    ln = getattr(node, 'lineno', None)
    return f" at line {ln}" if isinstance(ln, int) else ""


# --- reverse lookup tables --------------------------------------------------
# py comparison operator string -> EL operator (inverse of codegen.COMPARE_OPS).
_PY_TO_EL_CMP = {py: el for el, py in COMPARE_OPS.items()}

# pl_* runtime function -> a representative EL keyword. BUILTIN_FUNC_MAP is
# many-to-one; first EL spelling wins. The verify_twoway comparator canonicalises
# call names back through BUILTIN_FUNC_MAP, so any representative is behaviourally
# and structurally sound (all aliases collapse to the same py_name).
_PL_TO_EL = {}
for _el, _py in BUILTIN_FUNC_MAP.items():
    if _py.startswith('pl_'):
        _PL_TO_EL.setdefault(_py, _el)

# EL names whose forward form injects high/low/close ahead of the single length arg
# (codegen.py:1306-1333). Reverse strips those three leading series args.
_HLC_INJECTED = {
    'pl_cci': 'cci', 'pl_atr': 'atr', 'pl_adx': 'adx', 'pl_adxr': 'adxr',
    'pl_dmiplus': 'dmiplus', 'pl_dmiminus': 'dmiminus',
    'pl_fast_k': 'fastk', 'pl_fast_d': 'fastd',
    'pl_slow_k': 'slowk', 'pl_slow_d': 'slowd',
}

# EL names that map to __period_data__ (OpenD/HighD/.../CloseW ...) — forward emits
# `kwargs.get('<name>_<offset>', 0)`.
_PERIOD_NAMES = {el for el, py in BUILTIN_FUNC_MAP.items() if py == '__period_data__'}

# Fixed inline expansions of the "bare computed" reserved words (codegen.py:1152-1160).
# Matched by ast.unparse of the sub-expression (robust to spacing).
_BARE_COMPUTED_UNPARSE = {
    '(open[-1] + high[-1] + low[-1] + close[-1]) / 4.0': 'avgprice',
    '(high[-1] + low[-1]) / 2.0': 'medianprice',
    '(high[-1] + low[-1] + close[-1]) / 3.0': 'typicalprice',
    '(high[-1] + low[-1] + 2 * close[-1]) / 4.0': 'weightedclose',
    'high[-1] - low[-1]': 'range',
    'max(high[-1], close[-2] if close[-2] != 0.0 else open[-1])': 'truehigh',
    'min(low[-1], close[-2] if close[-2] != 0.0 else open[-1])': 'truelow',
    'pl_true_range(high[-1], low[-1], close[-2] if close[-2] != 0.0 else open[-1])':
        'truerange',
}

# Prologue assignment targets (codegen.py:706-727) — consumed, never program body.
_PROLOGUE_TARGETS = {
    '_orders', '_plots', '_alerts', '_risk', '_commentary', '_drawings',
    '_market_position', '_current_contracts', 'time_s', '_first_bar', '_trace',
}

# _risk['<key>'] = <value>  ->  (risk-func, value-interpretation)
_RISK_KEY_TO_FUNC = {
    'stop_contract': 'setstopcontract',
    'stop_share': 'setstopshare',
    'stop_position': 'setstopposition',
    'stop_loss': 'setstoploss',
    'profit_target': 'setprofittarget',
    'exit_on_close': 'setexitonclose',
    'dollar_trailing': 'setdollartrailing',
    'percent_trailing': 'setpercenttrailing',
    'breakeven': 'setbreakeven',
}

_ORDER_ACTIONS = {'buy', 'sell', 'sellshort', 'buytocover'}


# --- small ast helpers ------------------------------------------------------
def _is_name(node, ident=None):
    return isinstance(node, ast.Name) and (ident is None or node.id == ident)


def _is_call(node, func_name=None):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and (func_name is None or node.func.id == func_name))


def _is_attr_call(node, obj_name, attr):
    """Match `<obj_name>.<attr>(...)`."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr and _is_name(node.func.value, obj_name))


def _kwargs_get(node):
    """If `node` is `kwargs.get(<key>, <default>)`, return (key_str, default_node),
    else None."""
    if _is_attr_call(node, 'kwargs', 'get') and len(node.args) == 2:
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value, node.args[1]
    return None


def _const_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _neg_one(node):
    """Match the Python literal `-1` (UnaryOp USub of Constant 1)."""
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and node.operand.value == 1)


def _open_current(node):
    """Match `open[-1]` (the first-bar guard's else branch)."""
    return (isinstance(node, ast.Subscript) and _is_name(node.value, 'open')
            and _neg_one_index(node.slice))


def _neg_one_index(sl):
    return (isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub)
            and isinstance(sl.operand, ast.Constant) and sl.operand.value == 1)


# --- number reconstruction --------------------------------------------------
def _num(value_str):
    return {'type': 'number', 'value': value_str}


def _const_to_num(value):
    """A numeric Python constant -> a 'number' node with a faithful literal string.
    Booleans are handled earlier (bool is an int subclass)."""
    if isinstance(value, int):
        return _num(str(value))
    if isinstance(value, float):
        return _num(repr(value))
    raise PyFrontError(f"non-numeric constant where a number was expected: {value!r}")


# --- expression inversion ---------------------------------------------------
def inv_expr(node):
    """Invert a clean-dialect Python expression node into an AST expression dict.
    Raises PyFrontError on anything outside the dialect."""
    # ---- constants ----
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return {'type': 'boolean', 'value': v}
        if isinstance(v, str):
            return {'type': 'string', 'value': v}
        if isinstance(v, (int, float)):
            return _const_to_num(v)
        raise PyFrontError(f"unsupported constant {v!r}{_ln(node)}{_DIALECT_HINT}")

    # ---- names ----
    if isinstance(node, ast.Name):
        return {'type': 'ident', 'name': node.id}

    # ---- user-var series shim: (_state['X'] + [X]) -> ident X ----
    shim = _match_state_series(node)
    if shim is not None:
        return {'type': 'ident', 'name': shim}

    # ---- bare-computed reserved words (fixed inline expansions) ----
    try:
        unp = ast.unparse(node)
    except Exception:
        unp = None
    if unp is not None and unp in _BARE_COMPUTED_UNPARSE:
        return {'type': 'ident', 'name': _BARE_COMPUTED_UNPARSE[unp]}

    # ---- first-bar guard IfExp -> bar_ref ----
    if isinstance(node, ast.IfExp):
        guarded = _match_guard_barref(node)
        if guarded is not None:
            return guarded
        # Sign(x): (1 if x>0 else (-1 if x<0 else 0))
        sign = _match_sign(node)
        if sign is not None:
            return sign
        raise PyFrontError(f"unrecognised conditional expression: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")

    # ---- subscripts (current bar, bar_ref, arrays) ----
    if isinstance(node, ast.Subscript):
        return _inv_subscript(node)

    # ---- unary ----
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return {'type': 'unaryop', 'op': '-', 'operand': inv_expr(node.operand)}
        if isinstance(node.op, ast.Not):
            return {'type': 'unaryop', 'op': 'not', 'operand': inv_expr(node.operand)}
        if isinstance(node.op, ast.UAdd):
            return inv_expr(node.operand)
        raise PyFrontError(f"unsupported unary op {type(node.op).__name__}{_ln(node)}{_DIALECT_HINT}")

    # ---- boolean and/or (folded left-assoc into binops) ----
    if isinstance(node, ast.BoolOp):
        word = 'and' if isinstance(node.op, ast.And) else 'or'
        acc = inv_expr(node.values[0])
        for v in node.values[1:]:
            acc = {'type': 'binop', 'op': word, 'left': acc, 'right': inv_expr(v)}
        return acc

    # ---- binary arithmetic ----
    if isinstance(node, ast.BinOp):
        dt = _match_datetime_expansion(node)
        if dt is not None:
            return dt
        return _inv_binop(node)

    # ---- comparison ----
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise PyFrontError(f"chained comparisons are outside the dialect{_ln(node)}")
        # `b in a` -> StrContains(a, b)
        if isinstance(node.ops[0], ast.In):
            return {'type': 'call', 'name': 'strcontains',
                    'args': [inv_expr(node.comparators[0]), inv_expr(node.left)]}
        op = node.ops[0]
        el = {ast.Eq: '=', ast.NotEq: '<>', ast.Lt: '<', ast.Gt: '>',
              ast.LtE: '<=', ast.GtE: '>='}.get(type(op))
        if el is None:
            raise PyFrontError(f"unsupported comparison op {type(op).__name__}{_ln(node)}{_DIALECT_HINT}")
        return {'type': 'compare', 'op': el,
                'left': inv_expr(node.left), 'right': inv_expr(node.comparators[0])}

    # ---- f-strings: NumToStr(expr, prec) ----
    if isinstance(node, ast.JoinedStr):
        return _inv_joinedstr(node)

    # ---- calls (built-ins, inline math, string ops) ----
    if isinstance(node, ast.Call):
        return _inv_call(node)

    raise PyFrontError(f"cannot invert expression: {ast.dump(node)}{_ln(node)}{_DIALECT_HINT}")


def _match_state_series(node):
    """`(_state['X'] + [X])` -> 'X', else None (a codegen series-context shim)."""
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Subscript)
            and _is_name(node.left.value, '_state')
            and isinstance(node.left.slice, ast.Constant)
            and isinstance(node.right, ast.List) and len(node.right.elts) == 1
            and _is_name(node.right.elts[0])):
        name = node.left.slice.value
        if node.right.elts[0].id == name:
            return name
    return None


def _strip_int(node):
    """Strip a codegen `int(...)` bound/index wrapper, returning the inner node."""
    if _is_call(node, 'int') and len(node.args) == 1:
        return node.args[0]
    return node


def _guard_index(operand):
    """From the `-(IDX + 1)` slice operand, return the index expression (IDX),
    stripping any `int(...)` wrapper. Raises if the shape is unexpected."""
    if not (isinstance(operand, ast.BinOp) and isinstance(operand.op, ast.Add)
            and isinstance(operand.right, ast.Constant) and operand.right.value == 1):
        raise PyFrontError(f"unexpected bar-ref index shape: {ast.dump(operand)}{_ln(operand)}")
    return _strip_int(operand.left)


def _match_guard_barref(node):
    """`(<s>[-(n+1)] if __builtin_len(<s>) > 1+n else open[-1])` -> bar_ref, else None."""
    if not isinstance(node, ast.IfExp):
        return None
    if not _open_current(node.orelse):
        return None
    body = node.body
    if not (isinstance(body, ast.Subscript)
            and isinstance(body.slice, ast.UnaryOp)
            and isinstance(body.slice.op, ast.USub)):
        return None
    idx = _guard_index(body.slice.operand)
    return {'type': 'bar_ref', 'series': inv_expr(body.value),
            'index': inv_expr(idx)}


def _match_sign(node):
    """`(1 if X>0 else (-1 if X<0 else 0))` -> Sign(X), else None."""
    if not (isinstance(node, ast.IfExp)
            and isinstance(node.body, ast.Constant) and node.body.value == 1
            and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Gt)):
        return None
    inner = node.orelse
    if not (isinstance(inner, ast.IfExp) and _neg_one(inner.body)
            and isinstance(inner.orelse, ast.Constant) and inner.orelse.value == 0
            and isinstance(inner.test, ast.Compare)
            and isinstance(inner.test.ops[0], ast.Lt)):
        return None
    return {'type': 'call', 'name': 'sign', 'args': [inv_expr(node.test.left)]}


def _inv_subscript(node):
    """Invert Subscript forms: series current bar, bar_ref, and array element refs."""
    sl = node.slice
    # `<s>[-1]` -> ident <s> (current bar access of a data series or shim)
    if _neg_one_index(sl):
        return inv_expr(node.value)
    # `<s>[-(n+1)]` -> bar_ref(<s>, n)   (non-guarded series / arrays)
    if isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub):
        idx = _guard_index(sl.operand)
        return {'type': 'bar_ref', 'series': inv_expr(node.value),
                'index': inv_expr(idx)}
    # String-slice built-ins (codegen.py:1359-1365):
    if isinstance(sl, ast.Slice) and sl.step is None:
        s = inv_expr(node.value)
        lo, hi = sl.lower, sl.upper
        # LeftStr(s, n): s[:n]
        if lo is None and hi is not None:
            return {'type': 'call', 'name': 'leftstr', 'args': [s, inv_expr(hi)]}
        # RightStr(s, n): s[-n:]
        if hi is None and isinstance(lo, ast.UnaryOp) and isinstance(lo.op, ast.USub):
            return {'type': 'call', 'name': 'rightstr',
                    'args': [s, inv_expr(lo.operand)]}
        # MidStr(s, start, count): s[(start - 1):(start - 1) + count]
        if (isinstance(lo, ast.BinOp) and isinstance(lo.op, ast.Sub)
                and isinstance(lo.right, ast.Constant) and lo.right.value == 1
                and isinstance(hi, ast.BinOp) and isinstance(hi.op, ast.Add)
                and ast.unparse(hi.left) == ast.unparse(lo)):
            return {'type': 'call', 'name': 'midstr',
                    'args': [s, inv_expr(lo.left), inv_expr(hi.right)]}
    raise PyFrontError(f"unsupported subscript: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")


def _inv_binop(node):
    op = node.op
    # FracPortion(X): (X - int(X))
    if isinstance(op, ast.Sub) and _is_call(node.right, 'int') \
            and len(node.right.args) == 1 \
            and ast.unparse(node.left) == ast.unparse(node.right.args[0]):
        return {'type': 'call', 'name': 'fracportion', 'args': [inv_expr(node.left)]}
    # Power / Square: a ** b  (b == 2 -> Square, else Power)
    if isinstance(op, ast.Pow):
        if isinstance(node.right, ast.Constant) and node.right.value == 2:
            return {'type': 'call', 'name': 'square', 'args': [inv_expr(node.left)]}
        return {'type': 'call', 'name': 'power',
                'args': [inv_expr(node.left), inv_expr(node.right)]}
    word = {ast.Add: '+', ast.Sub: '-', ast.Mult: '*', ast.Div: '/',
            ast.Mod: '%'}.get(type(op))
    if word is None:
        raise PyFrontError(f"unsupported binary op {type(op).__name__}{_ln(node)}{_DIALECT_HINT}")
    return {'type': 'binop', 'op': word,
            'left': inv_expr(node.left), 'right': inv_expr(node.right)}


def _match_datetime_expansion(node):
    """Invert the inline date/time arithmetic the codegen expands (codegen.py:
    1383-1433). Returns a call dict or None. Matched before generic binop handling
    because these expansions use `//` (FloorDiv), which is never emitted for plain
    EL arithmetic (EL `/` is true division; integer truncation goes through int())."""
    # ELTimeToDateTime(X): (((int(X)//100)*3600 + (int(X)%100)*60) / 86400.0)
    if (isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant) and node.right.value == 86400.0
            and isinstance(node.left, ast.BinOp)
            and isinstance(node.left.op, ast.Add)):
        add = node.left

        def _int_x(m, opcls, mult_k):
            if (isinstance(m, ast.BinOp) and isinstance(m.op, ast.Mult)
                    and isinstance(m.right, ast.Constant) and m.right.value == mult_k
                    and isinstance(m.left, ast.BinOp) and isinstance(m.left.op, opcls)
                    and isinstance(m.left.right, ast.Constant)
                    and m.left.right.value == 100
                    and _is_call(m.left.left, 'int') and len(m.left.left.args) == 1):
                return m.left.left.args[0]
            return None

        xh = _int_x(add.left, ast.FloorDiv, 3600)
        xm = _int_x(add.right, ast.Mod, 60)
        if xh is not None and xm is not None \
                and ast.unparse(xh) == ast.unparse(xm):
            return {'type': 'call', 'name': 'eltimetodatetime',
                    'args': [inv_expr(xh)]}
    # Hour(X): (X // 100)
    if (isinstance(node.op, ast.FloorDiv)
            and isinstance(node.right, ast.Constant) and node.right.value == 100):
        return {'type': 'call', 'name': 'hour', 'args': [inv_expr(node.left)]}
    return None


def _inv_joinedstr(node):
    """`f"{expr:.{prec}f}"` -> NumToStr(expr, prec)."""
    if len(node.values) != 1 or not isinstance(node.values[0], ast.FormattedValue):
        raise PyFrontError(f"unsupported f-string: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")
    fv = node.values[0]
    spec = fv.format_spec
    if not isinstance(spec, ast.JoinedStr) or len(spec.values) != 3:
        raise PyFrontError(f"unsupported f-string format spec: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")
    dot, prec_fv, ff = spec.values
    if not (isinstance(dot, ast.Constant) and dot.value == '.'
            and isinstance(ff, ast.Constant) and ff.value == 'f'
            and isinstance(prec_fv, ast.FormattedValue)):
        raise PyFrontError(f"unsupported NumToStr format: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")
    return {'type': 'call', 'name': 'numtostr',
            'args': [inv_expr(fv.value), inv_expr(prec_fv.value)]}


def _inv_call(node):
    """Invert a Python Call node: f32 wrapper, kwargs.get keyword/data forms, inline
    math/string builtins, and pl_* runtime functions."""
    func = node.func

    # --- attribute calls: kwargs.get(...) and string methods ---
    if isinstance(func, ast.Attribute):
        kg = _kwargs_get(node)
        if kg is not None:
            return _inv_kwargs_get(*kg)
        # X.lower() / X.upper() -> LowerStr / UpperStr
        if func.attr == 'lower' and not node.args:
            return {'type': 'call', 'name': 'lowerstr', 'args': [inv_expr(func.value)]}
        if func.attr == 'upper' and not node.args:
            return {'type': 'call', 'name': 'upperstr', 'args': [inv_expr(func.value)]}
        # X.replace(a, b) -> StrReplace ; X.find(a) -> StrFind ; X.format(...) -> StringFormat
        if func.attr == 'replace' and len(node.args) == 2:
            return {'type': 'call', 'name': 'strreplace',
                    'args': [inv_expr(func.value)] + [inv_expr(a) for a in node.args]}
        if func.attr == 'find' and len(node.args) == 1:
            return {'type': 'call', 'name': 'strfind',
                    'args': [inv_expr(func.value), inv_expr(node.args[0])]}
        if func.attr == 'format':
            return {'type': 'call', 'name': 'stringformat',
                    'args': [inv_expr(func.value)] + [inv_expr(a) for a in node.args]}
        # math.log / math.sqrt / math.ceil / math.floor / math.exp
        if _is_name(func.value, 'math'):
            m = {'log': 'log', 'sqrt': 'sqrt', 'ceil': 'ceiling',
                 'floor': 'floor', 'exp': 'expvalue'}.get(func.attr)
            if m is not None:
                return {'type': 'call', 'name': m,
                        'args': [inv_expr(a) for a in node.args]}
        raise PyFrontError(f"unsupported attribute call: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")

    if not isinstance(func, ast.Name):
        raise PyFrontError(f"unsupported call target: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")

    name = func.id
    args = node.args

    # f32(x) — pure rounding wrapper, transparent to the AST
    if name == 'f32' and len(args) == 1:
        return inv_expr(args[0])
    # int(x) -> IntPortion(x)
    if name == 'int' and len(args) == 1:
        return {'type': 'call', 'name': 'intportion', 'args': [inv_expr(args[0])]}
    # abs(x) -> AbsValue(x)
    if name == 'abs' and len(args) == 1:
        return {'type': 'call', 'name': 'absvalue', 'args': [inv_expr(args[0])]}
    # float(x) -> StrToNum(x)
    if name == 'float' and len(args) == 1:
        return {'type': 'call', 'name': 'strtonum', 'args': [inv_expr(args[0])]}
    # __builtin_len(x) -> StrLen(x)
    if name == '__builtin_len' and len(args) == 1:
        return {'type': 'call', 'name': 'strlen', 'args': [inv_expr(args[0])]}
    # el_round0(x) -> Round(x) ; el_round(x, n) -> Round(x, n)
    if name in ('el_round0', 'el_round'):
        return {'type': 'call', 'name': 'round', 'args': [inv_expr(a) for a in args]}
    # max/min: truehigh/truelow handled via _BARE_COMPUTED; else MaxList/MinList
    if name == 'max':
        return {'type': 'call', 'name': 'maxlist', 'args': [inv_expr(a) for a in args]}
    if name == 'min':
        return {'type': 'call', 'name': 'minlist', 'args': [inv_expr(a) for a in args]}
    # CurrentSession / SessionLastBar / LastCalc*: kwargs.get default is the real call —
    # handled in _inv_kwargs_get; a bare pl_currentsession would only appear there.

    return _inv_pl_call(name, args)


def _inv_kwargs_get(key, default):
    """Invert a `kwargs.get(key, default)` value expression into position_kw / mc_kw /
    period-data call / data_of / capture-overridden call."""
    # Capture-overridden call: kwargs.get('name', pl_xxx(args)) -> the inner call.
    if isinstance(default, ast.Call):
        return _inv_call(default)
    # data_of: kwargs.get('data<N>_<series>', <series>[-1])
    if '_' in key and key.startswith('data'):
        head, _, tail = key.partition('_')
        num = head[len('data'):]
        if num.isdigit() and tail:
            return {'type': 'data_of', 'series': tail, 'data_num': int(num)}
    # period data: kwargs.get('<name>_<offset>', 0)
    head, sep, tail = key.rpartition('_')
    if sep and head in _PERIOD_NAMES and (tail.lstrip('-').isdigit()):
        return {'type': 'call', 'name': head, 'args': [_num(tail)]}
    # bare reserved-word data-number idents (codegen.py:1403-1406): the forward
    # transpiler renders CurrentDataNumber / BaseDataNumber as kwargs.get(...) but
    # the parser keeps them as plain idents.
    if key in _KWKEY_TO_IDENT:
        return {'type': 'ident', 'name': _KWKEY_TO_IDENT[key]}
    # EntryName(N) / ExitName(N): the forward transpiler DROPS the index arg
    # (codegen.py:1453-1456 return a fixed `kwargs.get('entry_name', '')`), so it is
    # not recoverable — reconstruct the canonical `0` index the GT corpus uses.
    if key in _KWKEY_TO_NAMECALL:
        return {'type': 'call', 'name': _KWKEY_TO_NAMECALL[key], 'args': [_num('0')]}
    # BarNumberOfData(N): kwargs.get('bar_number_of_data_{N}', 0)
    if key.startswith('bar_number_of_data_'):
        n = key[len('bar_number_of_data_'):]
        if n.lstrip('-').isdigit():
            return {'type': 'call', 'name': 'barnumberofdata', 'args': [_num(n)]}
    # position keyword / mc keyword
    if key in _INV_POSKW:
        return {'type': 'position_kw', 'kwarg': key}
    if key in _INV_MCKW:
        return {'type': 'mc_kw', 'kwarg': key}
    raise PyFrontError(f"unrecognised kwargs.get key {key!r}")


_KWKEY_TO_IDENT = {
    'current_data_number': 'currentdatanumber',
    'base_data_number': 'basedatanumber',
}

_KWKEY_TO_NAMECALL = {
    'entry_name': 'entryname',
    'exit_name': 'exitname',
}


def _inv_pl_call(name, args):
    """Invert a `pl_*` (or inline-expansion) function call by name."""
    # crossesabove / crossesbelow -> call nodes (emit_el re-infixes them)
    if name == 'pl_crosses_above':
        return {'type': 'call', 'name': 'crossesabove',
                'args': [inv_expr(a) for a in args]}
    if name == 'pl_crosses_below':
        return {'type': 'call', 'name': 'crossesbelow',
                'args': [inv_expr(a) for a in args]}
    # CountIf: forward hardcodes the close>open comparison and drops the EL condition
    # (codegen.py:1319-1320). Reconstruct that fixed condition so the node round-trips.
    if name == 'pl_countif_window' and len(args) == 3:
        cond = {'type': 'compare', 'op': '>',
                'left': {'type': 'ident', 'name': 'close'},
                'right': {'type': 'ident', 'name': 'open'}}
        return {'type': 'call', 'name': 'countif', 'args': [cond, inv_expr(args[2])]}
    # Position-trade introspection: pl_pos_trade_field(kwargs, '<elname>', a, b)
    # (codegen emits this for PosTradeEntryName/Price/Bar/... ). The 2nd arg is the
    # EL function name; the remaining args are its EL arguments.
    if name == 'pl_pos_trade_field' and len(args) >= 2 \
            and _is_name(args[0], 'kwargs') and isinstance(args[1], ast.Constant):
        return {'type': 'position_kw_call', 'kwarg': args[1].value,
                'args': [inv_expr(a) for a in args[2:]]}
    # high/low/close-injected indicators: strip the three leading series args.
    if name in _HLC_INJECTED:
        el = _HLC_INJECTED[name]
        rest = args[3:]
        return {'type': 'call', 'name': el, 'args': [inv_expr(a) for a in rest]}
    # Historical function reference func(<series>[:-N], ...)[N] -> bar_ref(call, N).
    hist = _match_hist_funcref(name, args)
    if hist is not None:
        return hist
    if not name.startswith('pl_'):
        raise PyFrontError(f"unsupported function {name}(...)")
    el = _PL_TO_EL.get(name, name[len('pl_'):])
    # Zero-arg pl_* emissions are bare reserved-word properties (Category, Symbol,
    # IntervalType, CurrentTime_s, ExpirationDate, ...); the parser keeps them as
    # plain idents, so invert to an ident rather than an empty-arg call.
    if not args:
        return {'type': 'ident', 'name': el}
    return {'type': 'call', 'name': el, 'args': [inv_expr(a) for a in args]}


def _match_hist_funcref(name, args):
    """`pl_func(<series>[:-N], ...)[N]` is emitted for `Func(Series, ...)[N]`
    (codegen.py:1546-1561). Reverse: any sliced series arg -> bar_ref(call, N)."""
    offset = None
    for a in args:
        if (isinstance(a, ast.Subscript) and isinstance(a.slice, ast.Slice)
                and a.slice.lower is None and a.slice.step is None
                and isinstance(a.slice.upper, ast.UnaryOp)
                and isinstance(a.slice.upper.op, ast.USub)
                and isinstance(a.slice.upper.operand, ast.Constant)):
            offset = a.slice.upper.operand.value
            break
    if offset is None:
        return None
    de_sliced = []
    for a in args:
        if (isinstance(a, ast.Subscript) and isinstance(a.slice, ast.Slice)):
            de_sliced.append(a.value)   # the bare series Name
        else:
            de_sliced.append(a)
    inner = _inv_pl_call(name, de_sliced)
    return {'type': 'bar_ref', 'series': inner, 'index': _num(str(offset))}


# --- statement inversion ----------------------------------------------------
def _inv_stmts(pystmts):
    """Invert a list of Python statements (a function/if/for/while body) into a list
    of AST statement nodes. Handles the few multi-statement fan-outs (Stochastic)."""
    out = []
    i = 0
    n = len(pystmts)
    while i < n:
        stmt = pystmts[i]
        before = len(out)
        # `pass` is emitted two ways (codegen.py): as an empty-block filler (the SOLE
        # statement of an otherwise-empty if/for/while/once body) and as EL `Abort`
        # (`pass  # Abort`). The comment is gone by parse time, so disambiguate by
        # context: a lone `pass` is an empty body (contributes nothing); a `pass`
        # among other statements is an Abort.
        if isinstance(stmt, ast.Pass):
            if n > 1:
                out.append({'type': 'abort'})
            _stamp_line(out[before:], stmt.lineno)
            i += 1
            continue
        # Stochastic fan-out: `_stoch_result = pl_stochastic(...)` + N `v = f32(_stoch_result[k])`.
        consumed = _try_stochastic(pystmts, i, out)
        if consumed:
            _stamp_line(out[before:], stmt.lineno)
            i += consumed
            continue
        _inv_stmt(stmt, out)
        # `_line` = the Python source line, so codegen_el._anchor_comments places each
        # `#|` comment beside the same statement the forward codegen emitted it against
        # (nested statements are stamped by the recursive _inv_stmts on their own body).
        _stamp_line(out[before:], stmt.lineno)
        i += 1
    return out


def _try_stochastic(pystmts, i, out):
    """If a Stochastic fan-out begins at index i, append its single reconstructed
    assign to `out` and return the number of Python statements consumed; else 0."""
    s = pystmts[i]
    if not (isinstance(s, ast.Assign) and len(s.targets) == 1
            and _is_name(s.targets[0], '_stoch_result')
            and _is_call(s.value, 'pl_stochastic')):
        return 0
    input_args = [inv_expr(a) for a in s.value.args]
    # Following lines: v = f32(_stoch_result[k]) (or bare _stoch_result[k]).
    outputs = []          # (index_k, target_name)
    j = i + 1
    while j < len(pystmts):
        nxt = pystmts[j]
        info = _match_stoch_unpack(nxt)
        if info is None:
            break
        outputs.append(info)
        j += 1
    if not outputs:
        raise PyFrontError(f"pl_stochastic without result unpacking{_ln(s)}")
    outputs.sort(key=lambda t: t[0])
    # [0] is the return target; [1..] are the four output var names appended to args.
    ret_target = None
    output_names = []
    for k, tname in outputs:
        if k == 0:
            ret_target = tname
        else:
            output_names.append(tname)
    if ret_target is None:
        raise PyFrontError(f"pl_stochastic unpack missing result[0]{_ln(s)}")
    call = {'type': 'call', 'name': 'stochastic',
            'args': input_args + [{'type': 'ident', 'name': nm} for nm in output_names]}
    out.append({'type': 'assign',
                'target': {'type': 'ident', 'name': ret_target}, 'value': call})
    return j - i


def _match_stoch_unpack(stmt):
    """`v = f32(_stoch_result[k])` or `v = _stoch_result[k]` -> (k, 'v'), else None."""
    if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
            and _is_name(stmt.targets[0])):
        return None
    val = stmt.value
    if _is_call(val, 'f32') and len(val.args) == 1:
        val = val.args[0]
    if (isinstance(val, ast.Subscript) and _is_name(val.value, '_stoch_result')
            and isinstance(val.slice, ast.Constant)
            and isinstance(val.slice.value, int)):
        return val.slice.value, stmt.targets[0].id
    return None


def _inv_stmt(stmt, out):
    """Invert a single Python statement, appending 0+ AST nodes to `out`."""
    if isinstance(stmt, ast.Assign):
        _inv_assign(stmt, out)
        return
    if isinstance(stmt, ast.If):
        out.append(_inv_if(stmt))
        return
    if isinstance(stmt, ast.For):
        out.append(_inv_for(stmt))
        return
    if isinstance(stmt, ast.While):
        out.append(_inv_while(stmt))
        return
    if isinstance(stmt, ast.Expr):
        _inv_expr_stmt(stmt, out)
        return
    if isinstance(stmt, ast.Break):
        out.append({'type': 'break'})
        return
    if isinstance(stmt, ast.Continue):
        out.append({'type': 'continue'})
        return
    if isinstance(stmt, ast.Return):
        return  # the fixed footer `return {...}` — no AST node
    if isinstance(stmt, ast.Pass):
        return  # empty body filler
    raise PyFrontError(f"unsupported statement: {ast.unparse(stmt)}{_ln(stmt)}{_DIALECT_HINT}")


def _inv_assign(stmt, out):
    if len(stmt.targets) != 1:
        raise PyFrontError(f"multi-target assignment is outside the dialect{_ln(stmt)}")
    target = stmt.targets[0]
    # _risk['key'] = value  -> risk node
    if (isinstance(target, ast.Subscript) and _is_name(target.value, '_risk')
            and isinstance(target.slice, ast.Constant)):
        out.append(_inv_risk_assign(target.slice.value, stmt.value))
        return
    # Assign to a Name or an array element (bar_ref subscript target).
    if _is_name(target):
        tnode = {'type': 'ident', 'name': target.id}
    elif isinstance(target, ast.Subscript):
        tnode = _inv_subscript(target)
    else:
        raise PyFrontError(f"unsupported assignment target: {ast.unparse(target)}{_ln(target)}{_DIALECT_HINT}")
    out.append({'type': 'assign', 'target': tnode, 'value': inv_expr(stmt.value)})


def _inv_risk_assign(key, value):
    func = _RISK_KEY_TO_FUNC.get(key)
    if func is None:
        raise PyFrontError(f"unrecognised _risk key {key!r}{_ln(value)}")
    # Boolean-flag risks: value is True and there is no argument.
    if func in ('setstopcontract', 'setstopshare', 'setexitonclose'):
        return {'type': 'risk', 'func': func, 'arg': None}
    if func == 'setstopposition':
        if isinstance(value, ast.Constant) and value.value is True:
            return {'type': 'risk', 'func': func, 'arg': None}
        return {'type': 'risk', 'func': func, 'arg': inv_expr(value)}
    if func == 'setpercenttrailing':
        if not (isinstance(value, ast.Tuple) and len(value.elts) == 2):
            raise PyFrontError(f"SetPercentTrailing expects a 2-tuple{_ln(value)}")
        return {'type': 'risk', 'func': func,
                'args': [inv_expr(value.elts[0]), inv_expr(value.elts[1])]}
    return {'type': 'risk', 'func': func, 'arg': inv_expr(value)}


def _inv_if(stmt):
    # Once: `if _first_bar: <body>; _first_bar = False`
    if _is_first_bar(stmt.test) and not stmt.orelse:
        body = stmt.body
        if not body or not _is_first_bar_reset(body[-1]):
            raise PyFrontError(f"`if _first_bar:` without trailing `_first_bar = False`{_ln(stmt)}")
        return {'type': 'once', 'body': _inv_stmts(body[:-1])}
    cond = inv_expr(stmt.test)
    body = _inv_stmts(stmt.body)
    else_body = _inv_stmts(stmt.orelse) if stmt.orelse else None
    return {'type': 'if', 'cond': cond, 'body': body, 'else_body': else_body}


def _is_first_bar(node):
    return _is_name(node, '_first_bar')


def _is_first_bar_reset(stmt):
    return (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
            and _is_name(stmt.targets[0], '_first_bar')
            and isinstance(stmt.value, ast.Constant) and stmt.value.value is False)


def _inv_for(stmt):
    """`for v in range(int(a), int(b) + 1):` -> for-to ; `range(int(a), int(b) - 1, -1)`
    -> for-downto (codegen.py:861-865)."""
    if not _is_name(stmt.target):
        raise PyFrontError(f"for-loop target must be a simple name{_ln(stmt)}")
    call = stmt.iter
    if not _is_call(call, 'range'):
        raise PyFrontError(f"for-loop must iterate a range(...){_ln(stmt)}")
    var = stmt.target.id
    if len(call.args) == 2:
        start = _strip_int(call.args[0])
        end = _strip_end_bound(call.args[1], downto=False)
        downto = False
    elif len(call.args) == 3:
        step = call.args[2]
        if not (isinstance(step, ast.UnaryOp) and isinstance(step.op, ast.USub)
                and isinstance(step.operand, ast.Constant) and step.operand.value == 1):
            raise PyFrontError(f"downto range must step by -1{_ln(stmt)}")
        start = _strip_int(call.args[0])
        end = _strip_end_bound(call.args[1], downto=True)
        downto = True
    else:
        raise PyFrontError(f"unsupported range() arity in for-loop{_ln(stmt)}")
    return {'type': 'for', 'var': var,
            'start': inv_expr(start), 'end': inv_expr(end),
            'downto': downto, 'body': _inv_stmts(stmt.body)}


def _strip_end_bound(node, downto):
    """From `int(end) + 1` (to) or `int(end) - 1` (downto), return the `end` node."""
    want = ast.Sub if downto else ast.Add
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, want)
            and isinstance(node.right, ast.Constant) and node.right.value == 1):
        raise PyFrontError(f"unexpected for-loop end bound: {ast.unparse(node)}{_ln(node)}")
    return _strip_int(node.left)


def _inv_while(stmt):
    """Regular `while (cond):` -> while ; the repeat-until shim
    `while True: <body>; if C: break` -> repeat_until (codegen.py:887-897)."""
    if isinstance(stmt.test, ast.Constant) and stmt.test.value is True:
        body = stmt.body
        if (body and isinstance(body[-1], ast.If) and not body[-1].orelse
                and len(body[-1].body) == 1 and isinstance(body[-1].body[0], ast.Break)):
            cond = inv_expr(body[-1].test)
            return {'type': 'repeat_until', 'body': _inv_stmts(body[:-1]), 'cond': cond}
        raise PyFrontError(f"`while True:` without a trailing `if C: break` (repeat-until){_ln(stmt)}")
    return {'type': 'while', 'cond': inv_expr(stmt.test),
            'body': _inv_stmts(stmt.body)}


def _inv_expr_stmt(stmt, out):
    """Invert an expression statement: order append, plot append/noplot, print/alert,
    commentary, or a bare call (e.g. money-management point-based helpers)."""
    val = stmt.value
    # _orders.append((...)) -> order
    if _is_attr_call(val, '_orders', 'append') and len(val.args) == 1 \
            and isinstance(val.args[0], ast.Tuple):
        out.append(_inv_order(val.args[0].elts))
        return
    # _alerts.append(msg) -> alert
    if _is_attr_call(val, '_alerts', 'append') and len(val.args) == 1:
        out.append({'type': 'alert', 'msg': inv_expr(val.args[0])})
        return
    # _plots.setdefault('name', []).append(value) -> plot
    plot = _match_plot_append(val)
    if plot is not None:
        out.append(plot)
        return
    # _plots.__setitem__(f'plot{N}_noplot', True) / _color / _width / _style
    setplot = _match_setplot(val)
    if setplot is not None:
        out.append(setplot)
        return
    # print(...) -> print ; print(pl_file(path), msg) included
    if _is_call(val, 'print'):
        out.append({'type': 'print', 'args': [inv_expr(a) for a in val.args]})
        return
    # _commentary = ... is an Assign, not here; Commentary() would be a bare call.
    if isinstance(val, ast.Call):
        out.append({'type': 'expr_stmt', 'expr': inv_expr(val)})
        return
    raise PyFrontError(f"unsupported expression statement: {ast.unparse(stmt)}{_ln(stmt)}{_DIALECT_HINT}")


def _match_plot_append(val):
    """`_plots.setdefault('name', []).append(value)` -> plot node, else None."""
    if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
            and val.func.attr == 'append' and len(val.args) == 1):
        return None
    inner = val.func.value
    if not (_is_attr_call(inner, '_plots', 'setdefault') and len(inner.args) == 2
            and _const_str(inner.args[0])):
        return None
    return {'type': 'plot', 'name': inner.args[0].value,
            'value': inv_expr(val.args[0]), 'label': None}


def _match_setplot(val):
    """`_plots.__setitem__(f'plot{N}_<attr>', <v>)` -> the matching output call."""
    if not (_is_attr_call(val, '_plots', '__setitem__') and len(val.args) == 2):
        return None
    key = val.args[0]
    if not isinstance(key, ast.JoinedStr):
        return None
    # key text like: 'plot' <FormattedValue N> '_noplot'
    parts = key.values
    if not (len(parts) == 3 and isinstance(parts[0], ast.Constant)
            and parts[0].value == 'plot' and isinstance(parts[1], ast.FormattedValue)
            and isinstance(parts[2], ast.Constant)):
        return None
    suffix = parts[2].value
    n = inv_expr(parts[1].value)
    v = val.args[1]
    if suffix == '_noplot':
        return {'type': 'expr_stmt',
                'expr': {'type': 'call', 'name': 'noplot', 'args': [n]}}
    m = {'_color': 'setplotcolor', '_width': 'setplotwidth', '_style': 'setplotstyle'}
    if suffix in m:
        return {'type': 'expr_stmt',
                'expr': {'type': 'call', 'name': m[suffix],
                         'args': [n, inv_expr(v)]}}
    return None


def _inv_order(elts):
    """Invert an `_orders.append((...))` tuple into an order node (codegen.py:1033-1068)."""
    _oln = _ln(elts[0]) if elts else ""
    if len(elts) < 5:
        raise PyFrontError(f"order tuple too short{_oln}")
    # The `total` modifier rides as a trailing sentinel string 'total' (codegen
    # gen_order appends it only when set). Strip it back to node['total'] before
    # the positional price/entry_label parsing of the remaining elements.
    total = False
    if isinstance(elts[-1], ast.Constant) and elts[-1].value == 'total':
        total = True
        elts = elts[:-1]
    action = _const(elts[0])
    otype = _const(elts[1])
    if action not in _ORDER_ACTIONS:
        raise PyFrontError(f"unknown order action {action!r}{_oln}")
    node = {'type': 'order', 'action': action, 'order_type': otype,
            'bar_timing': _const(elts[3])}
    # Mirror the parser: the 'total' key is present only when the modifier is set
    # (parser.py:1191-1192), so a non-total order's AST stays key-for-key identical.
    if total:
        node['total'] = True
    # label (5th): '' -> no label; a bare string -> string node; else expression.
    label = elts[4]
    if isinstance(label, ast.Constant) and label.value == '':
        node['label'] = ''
    elif _const_str(label):
        node['label'] = {'type': 'string', 'value': label.value}
    else:
        node['label'] = inv_expr(label)
    # quantity (3rd): 0 -> None ; _current_contracts -> 'all' ; else the value.
    qty = elts[2]
    if isinstance(qty, ast.Constant) and qty.value == 0:
        node['quantity'] = None
    elif _is_name(qty, '_current_contracts'):
        node['quantity'] = {'type': 'ident', 'name': 'all'}
    else:
        node['quantity'] = inv_expr(qty)
    # optional price / entry_label (6th, 7th).
    node['entry_label'] = None
    node['price'] = None
    rest = elts[5:]
    if len(rest) == 1:
        if otype in ('limit', 'stop'):
            node['price'] = inv_expr(rest[0])
        elif _const_str(rest[0]):
            node['entry_label'] = _label_node(rest[0])
        else:
            node['price'] = inv_expr(rest[0])
    elif len(rest) == 2:
        node['price'] = inv_expr(rest[0])
        node['entry_label'] = _label_node(rest[1])
    elif len(rest) > 2:
        raise PyFrontError(f"order tuple too long{_oln}")
    return node


def _label_node(node):
    if _const_str(node):
        return {'type': 'string', 'value': node.value}
    return inv_expr(node)


def _const(node):
    if not isinstance(node, ast.Constant):
        raise PyFrontError(f"expected a constant, got {ast.dump(node)}{_ln(node)}")
    return node.value


# --- module / prologue framing ----------------------------------------------
def _find_strategy(module):
    """Return the `def strategy(...)` FunctionDef; raise if the module is not the
    expected clean-dialect shape."""
    fn = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef):
            if node.name != 'strategy':
                raise PyFrontError(f"unexpected function def {node.name!r}{_ln(node)}")
            if fn is not None:
                raise PyFrontError(f"more than one function definition{_ln(node)}")
            fn = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            continue  # the fixed runtime import header
        elif isinstance(node, ast.Assign):
            # `__builtin_len = len` — the only module-level assign.
            if not (len(node.targets) == 1 and _is_name(node.targets[0], '__builtin_len')):
                raise PyFrontError(f"unexpected module-level assign: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")
        else:
            raise PyFrontError(f"unexpected module-level statement: {ast.unparse(node)}{_ln(node)}{_DIALECT_HINT}")
    if fn is None:
        raise PyFrontError("no `def strategy(...)` found")
    return fn


def _skip_prologue(body):
    """Skip the fixed prologue (the isinstance guard + the `_*`/time_s inits) and
    return the index of the first program statement."""
    i = 0
    n = len(body)
    while i < n:
        stmt = body[i]
        # `if not isinstance(close, list): ...` — the series-coercion guard.
        if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.UnaryOp) \
                and isinstance(stmt.test.op, ast.Not):
            i += 1
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and _is_name(stmt.targets[0]) \
                and stmt.targets[0].id in _PROLOGUE_TARGETS:
            i += 1
            continue
        break
    return i


def _parse_inputs(body, i, out, declared):
    """Consume the input block: `Name = kwargs.get('Name', default)` (key == target)."""
    n = len(body)
    while i < n:
        stmt = body[i]
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and _is_name(stmt.targets[0])):
            break
        kg = _kwargs_get(stmt.value)
        if kg is None or kg[0] != stmt.targets[0].id:
            break
        name = stmt.targets[0].id
        default = kg[1]
        out.append({'type': 'input_decl', '_line': stmt.lineno,
                    'decls': [{'name': name, 'default': inv_expr(default)}]})
        declared.add(name)
        i += 1
    return i


def _array_init_form(value):
    """If `value` is an array declaration initializer (`[init]` or
    `[init] * (size + 1)`), return {'init', 'size'}; else None."""
    if isinstance(value, ast.List):
        if len(value.elts) != 1:
            return None
        return {'init': inv_expr(value.elts[0]), 'size': None}
    if (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mult)
            and isinstance(value.left, ast.List) and len(value.left.elts) == 1):
        size = value.right
        if (isinstance(size, ast.BinOp) and isinstance(size.op, ast.Add)
                and isinstance(size.right, ast.Constant) and size.right.value == 1):
            return {'init': inv_expr(value.left.elts[0]), 'size': inv_expr(size.left)}
        return None
    return None


def _parse_decls(body, i, out, declared):
    """Consume the contiguous declaration block. The forward transpiler emits every
    declared Var/Array as one `Name = <init>` assignment, in declaration order,
    immediately after the input block and before the first executable statement. EL
    requires every variable to be declared before use, so no body statement ever
    introduces a NEW name — the block therefore ends at the first target that is
    already declared (a reassignment), the first non-`Name = expr` statement, or the
    first generated `_`-prefixed target (e.g. `_stoch_result`), whichever comes first.

    Array inits (`[x]` / `[x] * (size + 1)`) are recognised structurally; every other
    init is a Var declaration whose init is the inverted RHS (this covers
    non-literal inits such as `Vars: D2Close(Close)` -> `d2close = f32(f32(close[-1]))`,
    which the literal-only rule used to misclassify as executable assignments)."""
    n = len(body)
    while i < n:
        stmt = body[i]
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and _is_name(stmt.targets[0])):
            break
        name = stmt.targets[0].id
        if name in declared or name.startswith('_'):
            break
        arr = _array_init_form(stmt.value)
        if arr is not None:
            out.append({'type': 'array_decl', '_line': stmt.lineno,
                        'decls': [{'name': name, 'init': arr['init'],
                                   'size': arr['size'], 'intrabar_persist': False}]})
        else:
            out.append({'type': 'var_decl', '_line': stmt.lineno,
                        'decls': [{'name': name, 'init': inv_expr(stmt.value),
                                   'data_ref': None, 'intrabar_persist': False}]})
        declared.add(name)
        i += 1
    return i


def _recapture_comments(py_source):
    """Recover the verbatim EL comments the forward codegen (clean mode, DIALECT 3)
    embedded as `#| <verbatim>` markers, WITH their anchor coordinates. Python's `ast`
    discards comments, so this is a separate `tokenize` pass (robust to a `#|` inside a
    string literal, which `str.find` would misread).

    Each `#|` COMMENT token becomes one `_comments` entry carrying the SAME shape the
    parser's side-list uses ({'text','line','col','trailing'}), so the shared
    `codegen_el._anchor_comments` re-anchors it EXACTLY as the EL leg does:

      * `line`/`col` = the token's 1-based Python source position — paired with the
        `_line` stamped on each reconstructed statement (its Python lineno), the emitter
        anchors every comment to the same statement the forward codegen emitted it beside;
      * `trailing` = the `#|` shares its physical line with real code (a same-line
        trailing comment, DIALECT 3), vs sitting on its own line (a leading comment).

    A multi-line `{...}` block was emitted as consecutive `#|` lines and re-forms as a
    contiguous brace block on re-emit (each physical line is one entry, in order)."""
    import tokenize
    import io
    out = []
    code_rows = set()          # rows carrying a real (non-comment/-layout) token
    comments = []              # (row, col, text)
    try:
        toks = tokenize.generate_tokens(io.StringIO(py_source).readline)
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                if tok.string.startswith('#|'):
                    body = tok.string[2:]
                    if body.startswith(' '):
                        body = body[1:]
                    comments.append((tok.start[0], tok.start[1] + 1, body))
            elif tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                  tokenize.DEDENT, tokenize.ENCODING,
                                  tokenize.ENDMARKER):
                code_rows.add(tok.start[0])
    except tokenize.TokenError:
        # A truncated tokenization still yields the comments seen so far; the AST
        # parse (ast.parse in py_to_ast) is the authoritative fail-loud path.
        pass
    for row, col, text in comments:
        out.append({'text': text, 'line': row, 'col': col,
                    'trailing': row in code_rows})
    return out


def _stamp_line(nodes, lineno):
    """Stamp `_line` (the Python source line) on each freshly-built AST statement in
    `nodes` so `codegen_el._anchor_comments` can re-anchor comments to it. `setdefault`
    never clobbers a nested statement already stamped by a recursive `_inv_stmts`."""
    for node in nodes:
        if isinstance(node, dict):
            node.setdefault('_line', lineno)


def py_to_ast(py_source):
    """Parse clean-dialect Python source into the parser's dict AST (a 'program' node).
    Raises PyFrontError on any construct outside the mirror dialect."""
    try:
        module = ast.parse(py_source)
    except SyntaxError as e:
        raise PyFrontError(f"input is not valid Python: {e}{_ln(e)}")
    fn = _find_strategy(module)
    body = fn.body
    out = []
    declared = set()
    i = _skip_prologue(body)
    i = _parse_inputs(body, i, out, declared)
    i = _parse_decls(body, i, out, declared)
    out.extend(_inv_stmts(body[i:]))
    return {'type': 'program', 'body': out, '_comments': _recapture_comments(py_source)}
