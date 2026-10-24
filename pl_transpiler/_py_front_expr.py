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


"""Expression inversion for py_front: clean-dialect Python expression nodes back
to AST expression dicts (see `py_front`)."""

import ast
from typing import Optional, cast

from pl_transpiler._py_front_common import (
    _BARE_COMPUTED_UNPARSE,
    _DIALECT_HINT,
    _HLC_INJECTED,
    _PERIOD_NAMES,
    _PL_TO_EL,
    PyFrontError,
    _const_to_num,
    _is_call,
    _is_name,
    _kwargs_get,
    _ln,
    _neg_one,
    _neg_one_index,
    _num,
    _open_current,
)
from pl_transpiler._types import Node
from pl_transpiler.codegen_el import _INV_MCKW, _INV_POSKW


# --- expression inversion ---------------------------------------------------
def inv_expr(node: ast.expr) -> Node:
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
        for operand in node.values[1:]:
            acc = {'type': 'binop', 'op': word, 'left': acc, 'right': inv_expr(operand)}
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


def _match_state_series(node: ast.expr) -> Optional[str]:
    """`(_state['X'] + [X])` -> 'X', else None (a codegen series-context shim)."""
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Subscript)
            and _is_name(node.left.value, '_state')
            and isinstance(node.left.slice, ast.Constant)
            and isinstance(node.right, ast.List) and len(node.right.elts) == 1
            and _is_name(node.right.elts[0])):
        name = cast(str, node.left.slice.value)  # str key: _state[...] subscripts are names
        if node.right.elts[0].id == name:
            return name
    return None


def _strip_int(node: ast.expr) -> ast.expr:
    """Strip a codegen `int(...)` bound/index wrapper, returning the inner node."""
    if _is_call(node, 'int') and len(node.args) == 1:
        return node.args[0]
    return node


def _guard_index(operand: ast.expr) -> ast.expr:
    """From the `-(IDX + 1)` slice operand, return the index expression (IDX),
    stripping any `int(...)` wrapper. Raises if the shape is unexpected."""
    if not (isinstance(operand, ast.BinOp) and isinstance(operand.op, ast.Add)
            and isinstance(operand.right, ast.Constant) and operand.right.value == 1):
        raise PyFrontError(f"unexpected bar-ref index shape: {ast.dump(operand)}{_ln(operand)}")
    return _strip_int(operand.left)


def _match_guard_barref(node: ast.expr) -> Optional[Node]:
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


def _match_sign(node: ast.expr) -> Optional[Node]:
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


def _inv_subscript(node: ast.Subscript) -> Node:
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


def _inv_binop(node: ast.BinOp) -> Node:
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


def _match_datetime_expansion(node: ast.BinOp) -> Optional[Node]:
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

        def _int_x(m: ast.expr, opcls: type[ast.operator], mult_k: int) -> Optional[ast.expr]:
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


def _inv_joinedstr(node: ast.JoinedStr) -> Node:
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


def _inv_call(node: ast.Call) -> Node:
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


def _inv_kwargs_get(key: str, default: ast.expr) -> Node:
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


def _inv_pl_call(name: str, args: list[ast.expr]) -> Node:
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
    # Optional bar-data column: pl_bar_column(kwargs, '<kwarg>', '<EL word>')
    # (codegen emits this for Ticks/UpTicks/DownTicks/OpenInt).
    if name == 'pl_bar_column' and len(args) == 3 \
            and _is_name(args[0], 'kwargs') and isinstance(args[1], ast.Constant):
        return {'type': 'position_kw', 'kwarg': args[1].value}
    # Positions-ago keyword: pl_pos_ago(kwargs, '<kwarg>', n) -> bar_ref(position_kw, n)
    # (codegen emits this for EntryDate(n)/ExitPrice(n)/...).
    if name == 'pl_pos_ago' and len(args) == 3 \
            and _is_name(args[0], 'kwargs') and isinstance(args[1], ast.Constant):
        return {'type': 'bar_ref',
                'series': {'type': 'position_kw', 'kwarg': args[1].value},
                'index': inv_expr(args[2])}
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


def _match_hist_funcref(name: str, args: list[ast.expr]) -> Optional[Node]:
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
