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

"""PowerLanguage runtime: float32 narrowing, EL rounding and the unimplemented-builtin (loud stub)
machinery shared by every runtime submodule.

Re-exported by pl_runtime; see that module."""

import ctypes

# Module-level alias: dodge ctypes.c_float attribute lookup on the ~25-calls/bar path.
_CFloat = ctypes.c_float

def f32(x):
    """Narrow to IEEE-754 float32 to match EasyLanguage single-precision."""
    try:
        return _CFloat(x).value
    except (OverflowError, ValueError, TypeError):
        if x is None:
            return 0.0
        try:
            return _CFloat(float(x)).value
        except (OverflowError, ValueError, TypeError):
            return float(x)

def el_array_pos(arr, i):
    """List position of EL array element i.

    Generated code stores EL index i at list position -(i + 1) (see
    codegen_expr's subscript emission), so the last list item is A[0]. Raises
    IndexError for an index outside 0..len(arr) - 1."""
    i = int(i)
    if not 0 <= i < len(arr):
        raise IndexError(i)
    return -(i + 1)


def el_array_items(arr):
    """The elements of an EL array as a new list in EL index order."""
    return arr[::-1]


def el_array_store(arr, items):
    """Write items (in EL index order) back into arr in place."""
    arr[:] = items[::-1]


def el_round0(x):
    """EasyLanguage Round(x, 0): half-away-from-zero (NOT Python banker's rounding)."""
    import math
    if x >= 0:
        return math.floor(x + 0.5)
    else:
        return math.ceil(x - 0.5)

def el_round(x, decimals):
    """EasyLanguage Round(x, decimals): generalised half-away-from-zero."""
    import math
    factor = 10 ** decimals
    if x >= 0:
        return math.floor(x * factor + 0.5) / factor
    else:
        return math.ceil(x * factor - 0.5) / factor

# --- Unimplemented-stub instrumentation -------------------------------------
# Many EasyLanguage builtins are stubs: charting/UI, broker/account, DLL,
# portfolio, collections (all out of scope for a backtest), plus not-yet-built
# calc functions. To avoid SILENTLY wrong results, every stub records its call
# here and, by default, raises -- so an LLM-written strategy that touches an
# unimplemented function fails LOUDLY instead of producing plausible-but-wrong
# trades. Cosmetic stubs (drawing, sound, print; see _UNIMPL_COSMETIC below)
# never raise. PL_STRICT=0 (or `--lenient` on pl_run / pl_oracle) opts out:
# stubs then return 0/'' and the run ends with one stderr WARNING naming them.
import os as _os


def _strict_from_env(environ=None):
    """Strict unless PL_STRICT is set to 0 / false / no / off."""
    val = (_os.environ if environ is None else environ).get('PL_STRICT', '')
    return val.strip().lower() not in ('0', 'false', 'no', 'off')


_PL_STRICT = _strict_from_env()
_UNIMPL_CALLS = {}
# Builtins whose correct value is a constant 0, so their bodies look like stubs;
# they are not wrapped as loud stubs. Keep this minimal and explicit so nothing
# is hidden. TickID and ExecOffset matches MultiCharts backtests: 0 on every
# historical bar (checked against a MultiCharts capture).
_UNIMPL_WHITELIST = {'pl_tickid', 'pl_execoffset'}

# String-returning EL builtins. When these are unimplemented their loud stub
# must return '' (not the numeric 0 default), otherwise transpiled code that
# concatenates the result (Line = Line + Symbol) raises TypeError mid-bar and
# aborts the whole bar, zeroing every traced column. The non-strict fallback is
# type-correct; the strict path still raises so the gap stays visible.
_UNIMPL_STR_RETURNING = {
    'pl_datetimetostring', 'pl_datetimetostring_ms',
    'pl_formatdate', 'pl_formattime', 'pl_timetostring', 'pl_datetostring',
    'pl_array_getstringvalue',
}

class UnimplementedBuiltinError(NotImplementedError):
    """A strategy called an EasyLanguage builtin the runtime does not implement."""

    def __init__(self, name):
        self.builtin = name[3:] if name.startswith('pl_') else name
        super().__init__(
            "EasyLanguage builtin '%s' is not implemented in the PowerBridge runtime, "
            "so the run stopped instead of guessing 0/''; rerun with PL_STRICT=0 "
            "(or --lenient) to treat it as 0/'' and get a warning instead"
            % self.builtin)


class MissingBarColumnError(UnimplementedBuiltinError):
    """A strategy read a per-bar field (Ticks, OpenInt, ...) the bar data lacks."""

    def __init__(self, word, column):
        self.builtin = word
        self.column = column
        NotImplementedError.__init__(
            self,
            "EasyLanguage '%s' needs a '%s' column in the bar data, which this data "
            "does not have, so the run stopped instead of guessing 0; rerun with "
            "PL_STRICT=0 (or --lenient) to treat it as 0 and get a warning instead"
            % (word, column))


def pl_bar_column(kwargs, column, word):
    """Value of per-bar data column ``column`` (EL reserved word ``word``).

    Returns the value the bar loop supplied. When the data has no such column,
    strict mode raises MissingBarColumnError; lenient mode records the miss
    (reported by pl_unimplemented_warn) and returns 0."""
    if column in kwargs:
        return kwargs[column]
    name = 'pl_' + column
    _UNIMPL_CALLS[name] = _UNIMPL_CALLS.get(name, 0) + 1
    if _PL_STRICT:
        raise MissingBarColumnError(word, column)
    return 0


def set_strict_mode(strict):
    """Set strict mode for this process (pl_run / pl_oracle --lenient use it)."""
    global _PL_STRICT
    _PL_STRICT = bool(strict)


def _unimplemented(name):
    _UNIMPL_CALLS[name] = _UNIMPL_CALLS.get(name, 0) + 1
    if _PL_STRICT and name not in _UNIMPL_WHITELIST and not _is_cosmetic(name):
        raise UnimplementedBuiltinError(name)
    return '' if name in _UNIMPL_STR_RETURNING else 0

def pl_unimplemented_report():
    """{name: call_count} of unimplemented functions hit since the last reset (coverage linter)."""
    return dict(_UNIMPL_CALLS)

def pl_unimplemented_reset():
    """Clear the unimplemented-call registry (call before a fresh strategy run)."""
    _UNIMPL_CALLS.clear()


# Placeholders whose 0/'' cannot change trades in a backtest (drawing, sound,
# print/log, colours, commentary, mouse, DLL types). Still recorded, not warned.
_UNIMPL_COSMETIC_PREFIXES = (
    'pl_arw_', 'pl_text_', 'pl_tl_', 'pl_mc_arw_', 'pl_mc_text_', 'pl_mc_tl_',
    'pl_tool_', 'pl_commentary', 'pl_mouseclick', 'pl_lp', 'pl_get_plot', 'pl_getplot',
)
_UNIMPL_COSMETIC = frozenset((
    'pl_atcommentarybar', 'pl_checkcommentary', 'pl_cleardebug', 'pl_clearprintlog',
    'pl_messagelog', 'pl_playsound', 'pl_printer', 'pl_plotpaintbar', 'pl_scrolltobar',
    'pl_processmouseevents', 'pl_setfpcompareaccuracy', 'pl_setmaxbarsback',
    'pl_setcustomfitnessvalue', 'pl_recalclastbarafter', 'pl_recalcpersist',
    'pl_fileappend', 'pl_filedelete', 'pl_gradientcolor', 'pl_rgb', 'pl_legacycolortorgb',
    'pl_legacycolorvalue', 'pl_rgbtolegacycolor', 'pl_getbackgroundcolor',
))


def _is_cosmetic(name):
    return name in _UNIMPL_COSMETIC or name.startswith(_UNIMPL_COSMETIC_PREFIXES)


def pl_unimplemented_warn(label='pl_transpiler', stream=None):
    """Print ONE loud stderr warning naming every non-cosmetic unimplemented
    builtin hit since the last reset, and return {name: calls} for those.
    The run used 0/'' for each, so any result depending on them is not the
    MultiCharts result. Only reached in lenient mode (PL_STRICT=0); the default
    raises on the first call instead."""
    import sys as _sys
    hit = {n: c for n, c in _UNIMPL_CALLS.items() if not _is_cosmetic(n)}
    if hit:
        names = ", ".join("%s (%d calls)" % (n[3:], c) for n, c in sorted(hit.items()))
        (stream or _sys.stderr).write(
            "[%s] WARNING: this run used %d unimplemented EasyLanguage builtin(s) that "
            "returned 0/'' instead of the MultiCharts value: %s. Results that depend on "
            "them are NOT trustworthy (lenient mode: drop --lenient / PL_STRICT=0 to stop on the first call).\n"
            % (label, len(hit), names))
    return {n[3:]: c for n, c in hit.items()}


# Position keyword -> FillEngine.closed_trades key, for the positions-ago form
# EntryDate(n) / ExitPrice(n) / ... (see pl_pos_ago).
_POS_AGO_FIELDS = {
    'entry_date': 'entry_date',
    'entry_time': 'entry_time',
    'entry_price': 'entry_px',
    'exit_date': 'exit_date',
    'exit_time': 'exit_time',
    'exit_price': 'exit_px',
    'maxshares': 'contracts',
}


def pl_pos_ago(kwargs, field, pos_ago):
    """Positions-ago form of a position keyword, e.g. EntryDate(n).

    n = 0 is the current position: the scalar kwarg the bare keyword reads.
    n >= 1 is the n-th most recently closed position, read from the runner's
    closed-position ledger (``kwargs['_closed_trades']``, FillEngine.closed_trades),
    so ExitDate(1) is the last closed trade whether or not a position is open.
    An index past the first closed position returns 0, as MultiCharts does.
    """
    try:
        n = int(pos_ago)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return kwargs.get(field, 0)
    closed = kwargs.get('_closed_trades') or ()
    if n > len(closed):
        return 0
    return closed[-n][_POS_AGO_FIELDS[field]]


def pl_pos_trade_field(kwargs, field, pos_ago=0, trade_number=0):
    """2-arg position keyword, e.g. PosTradeProfit(PosAgo, TradeNumber).

    Looks up a per-trade value supplied by the runner via kwargs. The runner
    may key these either generically (just the field name) or per (pos_ago,
    trade_number). When the runner supplies no
    value, this defaults to 0 so transpiled code executes.
    """
    try:
        pos_ago = int(pos_ago)
        trade_number = int(trade_number)
    except (TypeError, ValueError):
        pos_ago, trade_number = 0, 0
    keyed = kwargs.get((field, pos_ago, trade_number))
    if keyed is not None:
        return keyed
    # String-typed trade fields (names/categories rendered as text) must default
    # to '' so callers that concatenate them (Line = Line + PosTradeEntryName(...))
    # don't raise TypeError and abort the bar, zeroing the whole trace.
    default = '' if field in ('postradeentryname', 'postradeexitname') else 0
    return kwargs.get(field, default)


# --- Install loud stubs (pl_runtime calls this LAST, after every import) ----
# The runtime submodules whose sources are scanned for trivial pl_* stubs.
_RUNTIME_SUBMODULES = ('_rt_core', '_rt_indicators', '_rt_drawing',
                       '_rt_arrays_dates', '_rt_builtins_az', '_rt_datetime')


def _install_loud_stubs():
    """Wrap every trivial `return 0/None`/`pass` pl_* function so it records the
    call (and raises unless PL_STRICT=0) instead of silently returning 0. Real
    implementations (anything that returns an expression) are left untouched.

    The loud wrapper is bound on the pl_runtime facade and on every runtime
    submodule that holds the name, so calls from inside a submodule hit the loud
    version too, exactly as they did when all of this lived in one module."""
    import ast as _ast
    import sys as _sys
    pkg = __name__.rpartition('.')[0]
    facade = _sys.modules[pkg + '.pl_runtime']
    mods = [_sys.modules[pkg + '.' + n] for n in _RUNTIME_SUBMODULES]
    src_path = None
    _trees = []
    try:
        # Read the runtime sources with an EXPLICIT utf-8 codec, never the locale
        # default. On a legacy codepage (cp932/936/949) or an ascii/LC_ALL=C
        # locale the default codec would raise UnicodeDecodeError on any
        # non-ASCII byte in these files, which would SILENTLY disable every
        # loud stub (see the except below) -- the worst failure mode there is:
        # production pl_run would then return plausible-but-wrong 0/'' values.
        for _m in mods:
            src_path = _m.__file__
            with open(src_path, 'r', encoding='utf-8') as _f:
                _trees.append(_ast.parse(_f.read()))
    except Exception as _exc:
        # Fail-loud vs. fail-silent. In strict mode (the default) the whole point
        # is that unimplemented builtins RAISE; if we cannot even introspect the
        # source to install those loud stubs we MUST NOT continue as if all were
        # fine, so we re-raise. With PL_STRICT=0 we keep the historical tolerant
        # behaviour (leave the trivial stubs returning 0/'') but emit exactly
        # one loud stderr warning so the degraded state is never invisible.
        if _PL_STRICT:
            raise RuntimeError(
                "pl_runtime could not introspect its own source (%s) to install "
                "the fail-loud strict-mode stubs; refusing to continue, because "
                "unimplemented EasyLanguage builtins would then silently return "
                "plausible-but-wrong 0/'' values instead of raising. "
                "Original error: %r" % (src_path, _exc))
        _sys.stderr.write(
            "WARNING: pl_runtime could not introspect its own source (%r); the "
            "unimplemented-builtin loud stubs are NOT installed, so out-of-scope "
            "EasyLanguage builtins will silently return 0/'' instead of failing "
            "loud. Unset PL_STRICT to make this condition fatal.\n" % (_exc,))
        return  # tolerant fallback: leave stubs as-is (no worse than before)

    def _is_stub(fn):
        body = list(fn.body)
        if body and isinstance(body[0], _ast.Expr) and isinstance(getattr(body[0], 'value', None), _ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]  # drop docstring
        if len(body) == 1 and isinstance(body[0], _ast.Return):
            v = body[0].value
            return v is None or (isinstance(v, _ast.Constant) and v.value in (0, 0.0, None, ''))
        if len(body) == 1 and isinstance(body[0], _ast.Pass):
            return True
        return False

    names = {n.name for _tree in _trees for n in _ast.walk(_tree)
             if isinstance(n, _ast.FunctionDef) and n.name.startswith('pl_') and _is_stub(n)}

    def _make_loud(nm):
        def _loud(*args, **kwargs):
            return _unimplemented(nm)
        _loud.__name__ = nm
        _loud.__doc__ = "Unimplemented EL builtin (loud stub): records call; raises unless PL_STRICT=0."
        _loud._pl_loud_stub = True
        return _loud

    for nm in names:
        if nm in _UNIMPL_WHITELIST:
            continue
        loud = _make_loud(nm)
        setattr(facade, nm, loud)
        for _m in mods:
            if hasattr(_m, nm):
                setattr(_m, nm, loud)

