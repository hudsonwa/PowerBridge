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


"""Shared pieces of the py_front mirror-dialect reader: the error type, reverse
lookup tables and small Python-AST predicates (see `py_front`)."""

import ast

from pl_transpiler.codegen import BUILTIN_FUNC_MAP, COMPARE_OPS
from pl_transpiler.codegen_tables import CANONICAL_EL_NAME


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

# pl_* runtime function -> its EL keyword. BUILTIN_FUNC_MAP is many-to-one; the
# canonical MultiCharts spelling (CANONICAL_EL_NAME) wins, else the first EL
# spelling. The verify_twoway comparator canonicalises call names back through
# BUILTIN_FUNC_MAP, so all aliases collapse to the same py_name either way.
_PL_TO_EL: dict[str, str] = {}
for _el, _py in BUILTIN_FUNC_MAP.items():
    if _py.startswith('pl_'):
        _PL_TO_EL.setdefault(_py, CANONICAL_EL_NAME.get(_py, _el))

# EL names whose forward form injects high/low/close ahead of the single length arg
# (codegen.py:1306-1333). Reverse strips those three leading series args.
_HLC_INJECTED = {
    'pl_cci': 'cci', 'pl_atr': CANONICAL_EL_NAME['pl_atr'], 'pl_adx': 'adx', 'pl_adxr': 'adxr',
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
    'stop_loss_pt': 'setstoploss_pt',
    'profit_target_pt': 'setprofittarget_pt',
    'breakeven_pt': 'setbreakeven_pt',
    'percent_trailing_pt': 'setpercenttrailing_pt',
    'trailing_stop_pt': 'settrailingstop_pt',
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
