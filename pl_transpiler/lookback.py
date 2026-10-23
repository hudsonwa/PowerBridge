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

"""lookback.py -- MaxBarsBack helpers: the static price-series lookback scan
behind the one-time "lookback past available history" warning."""
import sys

from pl_transpiler import catalog
from pl_transpiler.codegen_tables import BUILTIN_FUNC_MAP
from pl_transpiler.lexer import tokenise
from pl_transpiler.parser import parse

# Price series whose short-history reads fall back to the current bar's Open
# (codegen_expr._gen_bar_ref; the native interpreter's history_or_open).
_PRICE_SERIES = frozenset(
    ('close', 'high', 'low', 'open', 'volume', 'ticks', 'upticks', 'downticks'))


def max_price_lookback(ast):
    """Largest constant ``n`` in a ``Close[n]``-style read of a price series.

    Only literal indexes are counted; ``Close[Length]`` is not resolved.
    """
    best = 0
    stack = [ast]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get('type') == 'bar_ref':
                series, index = node.get('series'), node.get('index')
                if (isinstance(series, dict) and series.get('type') == 'ident'
                        and str(series.get('name', '')).lower() in _PRICE_SERIES
                        and isinstance(index, dict) and index.get('type') == 'number'):
                    try:
                        best = max(best, int(float(index['value'])))
                    except (TypeError, ValueError):
                        pass
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return best


def _windowed_calls():
    """Builtins that read a window of past bars and return 0 (or a partial
    value) until it is full. Lower-cased call names.

    Derived from codegen's full-series call list and the runtime functions
    that take a length; the marker-mapped indicators (``__cci__`` etc.) and
    MultiCharts names the runtime lacks are listed by hand."""
    import inspect

    from pl_transpiler import codegen_tables
    from pl_transpiler.codegen_expr import _ExprMixin
    from pl_transpiler.runtime import pl_runtime

    names = set(_ExprMixin._SERIES_FUNCS)
    for name, target in codegen_tables.BUILTIN_FUNC_MAP.items():
        func = getattr(pl_runtime, target, None)
        try:
            params = inspect.signature(func).parameters if callable(func) else {}
        except (TypeError, ValueError):
            params = {}
        if any('len' in p for p in params):
            names.add(name)
    names.update((
        'adx', 'adxr', 'atr', 'avgtruerange', 'cci', 'dmiminus', 'dmiplus',
        'fastd', 'fastk', 'slowd', 'slowk', 'stochd', 'stochk', 'truerange',
        'median', 'parabolicsar', 'tsi'))
    names.discard('sortarray')
    names.discard('variancearray')
    return frozenset(names)


_WINDOWED_CALLS = _windowed_calls()

# Where each windowed builtin takes its length(s): a tuple of groups of
# 0-based argument positions. The past bars read are the sum over groups of
# the largest length in the group, so ``MACDSignal(Price, Fast, Slow, Sig)``
# (``((1, 2), (3,))``) counts max(Fast, Slow) + Sig. An int is a fixed count.
# A windowed call missing here (or passed too few arguments) is refused by
# ``--max-bars-back auto`` rather than guessed. MRO and KeltnerChannel are
# MultiCharts functions PowerBridge does not run; they are listed so auto
# reports their length instead of calling them user functions.
_LENGTH_ARGS = {
    **dict.fromkeys((
        'average', 'averagefc', 'averagetfc', 'bollingerband', 'countif',
        'highest', 'highestbar', 'keltnerchannel', 'linearregangle',
        'linearregslope', 'linearregvalue', 'lowest', 'lowestbar', 'median',
        'momentum', 'mro', 'rateofchange', 'roc', 'rsi', 'standarddev',
        'stddev', 'summation', 'variance', 'waverage', 'weightedaverage',
        'xaverage'), ((1,),)),
    **dict.fromkeys((
        'adx', 'adxr', 'atr', 'avgtruerange', 'cci', 'dmiminus', 'dmiplus',
        'fastk', 'stochk'), ((0,),)),
    **dict.fromkeys(('fastd', 'slowk'), ((0,), (1,))),
    **dict.fromkeys(('slowd', 'stochd'), ((0,), (1,), (2,))),
    **dict.fromkeys(('macd', 'macdvalue'), ((1, 2),)),
    **dict.fromkeys(('macddiff', 'macdsignal'), ((1, 2), (3,))),
    'correlation': ((2,),),
    'stochastic': ((3,), (4,), (5,)),
    'truerange': 1,
}

# Windowed builtins with no length argument to read (left out of the table).
_LENGTH_UNKNOWN = frozenset(('parabolicsar', 'tsi'))

# Bare reserved words that read the previous bar (TrueRange uses Close[1]).
_LOOKBACK_IDENTS = frozenset(('truerange', 'truehigh', 'truelow'))

# Windowed calls whose ``Name(Price, 1)`` reads only the current bar
# (Momentum/RSI/ROC with length 1 still read Price[1]).
_CURRENT_BAR_AT_1 = frozenset((
    'average', 'averagefc', 'averagetfc', 'highest', 'highestbar', 'lowest',
    'lowestbar', 'summation', 'waverage', 'weightedaverage', 'xaverage'))


WARMUP_NOTE = ("note: {tool}: strategy reads past bars and --max-bars-back is 0; "
               "fills may differ from MultiCharts during warm-up "
               "(set --max-bars-back N, or -q to hide this)")

# The same note when infer_max_bars_back() finds a number.
WARMUP_NOTE_AUTO = ("note: {tool}: strategy reads up to {n} past bars and "
                    "--max-bars-back is 0; fills may differ from MultiCharts "
                    "during warm-up (set --max-bars-back auto (= {n}), or -q "
                    "to hide this)")


def _base_name(name):
    """``name`` with a trailing ``FC`` dropped when that names a windowed call."""
    if name not in _WINDOWED_CALLS and name not in _LENGTH_ARGS and name.endswith('fc'):
        return name[:-2]
    return name


def _is_windowed(name):
    name = _base_name(name)
    return name in _WINDOWED_CALLS or name in _LENGTH_ARGS


def _is_number(node, pred):
    if not (isinstance(node, dict) and node.get('type') == 'number'):
        return False
    try:
        return pred(float(node.get('value') or 0))
    except (TypeError, ValueError):
        return False


def _bars_ago(tokens):
    """True for the ``X of N bar(s) ago`` form, N > 0 (kept out of the AST)."""
    words = [(kind, str(text).lower()) for kind, text in tokens]
    for i in range(len(words) - 3):
        if (words[i] == ('IDENT', 'of') and words[i + 1][0] == 'NUMBER'
                and words[i + 2][1] in ('bar', 'bars') and words[i + 3][1] == 'ago'
                and float(words[i + 1][1]) != 0):
            return True
    return False


def uses_lookback(el_source):
    """True if the strategy may read past bars: it indexes a series (``x[n]``
    with n not a literal 0, or ``x of n bars ago``), uses TrueRange, calls a
    windowed builtin (Average, Highest, an ``*FC`` variant, ...) with a length
    other than a literal 1, or calls a user function (which may index
    internally). The same scan as ``infer_max_bars_back``: True when that
    finds a lookback or cannot bound one."""
    return _scan(el_source) != 0


def note_warmup(el_source, max_bars_back, tool, quiet=False, n_bars=None):
    """CLI only: print the once-per-run warm-up note on stderr when the
    strategy uses lookbacks and MaxBarsBack is 0. Returns True if printed.

    Skipped when the run will print the short-history warning (same cause),
    so one run never prints both. ``quiet`` hides only this note."""
    if quiet or max_bars_back:
        return False
    n = _scan(el_source)
    if not n:
        return False
    if (n_bars is not None and max_bars_back < n_bars
            and short_history_bar(el_source, max_bars_back) is not None):
        return False
    n = n if isinstance(n, int) else 0
    note = WARMUP_NOTE_AUTO if n else WARMUP_NOTE
    print(note.format(tool=tool, n=n), file=sys.stderr)
    return True


class LookbackUnknown(ValueError):
    """``infer_max_bars_back`` could not bound the strategy's lookback."""


def _input_defaults(ast):
    """Lower-cased input name -> numeric literal default."""
    out = {}
    for stmt in ast.get('body') or []:
        if isinstance(stmt, dict) and stmt.get('type') == 'input_decl':
            for decl in stmt.get('decls') or []:
                default = decl.get('default')
                if isinstance(default, dict) and default.get('type') == 'number':
                    out[str(decl.get('name', '')).lower()] = float(default['value'])
    return out


def _length(node, arg, inputs):
    """A length argument's value: a number or an input's default."""
    if _is_number(arg, lambda v: v >= 0):
        return int(float(arg['value']))
    if (isinstance(arg, dict) and arg.get('type') == 'ident'
            and str(arg.get('name', '')).lower() in inputs):
        return int(inputs[str(arg['name']).lower()])
    raise LookbackUnknown(
        f"the length of {node.get('name')}() is not a number or input")


def _window(node, args, inputs):
    """Past bars a windowed builtin call reads, from _LENGTH_ARGS."""
    name = _base_name(str(node.get('name', '')).lower())
    spec = _LENGTH_ARGS.get(name)
    if isinstance(spec, int):
        return spec
    if spec is None or max(p for group in spec for p in group) >= len(args):
        raise LookbackUnknown(
            f"the length argument of {node.get('name')}() is not known")
    lengths = [[_length(node, args[p], inputs) for p in group] for group in spec]
    if name in _CURRENT_BAR_AT_1 and lengths[0][0] <= 1:
        return 0
    return sum(max(group) for group in lengths)


def _scan(el_source):
    """The one lookback scan behind the warm-up note and ``auto``: the
    longest lookback (an int, 0 if none) or the LookbackUnknown raised when
    it cannot be bounded."""
    tokens, positions = tokenise(el_source)
    if _bars_ago(tokens):
        return LookbackUnknown("it uses 'of N bars ago'")
    ast = parse(tokens, positions=positions, source=el_source)
    try:
        return _need(ast, _input_defaults(ast))
    except LookbackUnknown as exc:
        return exc


def _need(node, inputs):
    """Past bars an expression reads (see infer_max_bars_back)."""
    if isinstance(node, list):
        return max((_need(n, inputs) for n in node), default=0)
    if not isinstance(node, dict):
        return 0
    kind = node.get('type')
    name = str(node.get('name', '')).lower()
    if kind == 'bar_ref':
        index = node.get('index')
        if not _is_number(index, lambda v: v >= 0 and v == int(v)):
            raise LookbackUnknown("a series is indexed by a non-constant [n]")
        return int(float(index['value'])) + _need(node.get('series'), inputs)
    if kind == 'call':
        args = node.get('args') or []
        inner = _need(args, inputs)
        if name in ('crossesabove', 'crossesbelow', 'crossover', 'crossunder') and len(args) == 2:
            return 1 + inner
        if _is_windowed(name):
            return _window(node, args, inputs) + inner
        if name not in BUILTIN_FUNC_MAP and catalog.get(name) is None:
            raise LookbackUnknown(f"it calls the user function {node.get('name')}()")
        return inner
    if kind == 'ident' and name in _LOOKBACK_IDENTS:
        return 1
    return _need([v for k, v in node.items() if not str(k).startswith('_')], inputs)


def infer_max_bars_back(el_source):
    """The MaxBarsBack that ``--max-bars-back auto`` uses: the longest
    lookback found by a static scan, or 0 if ``uses_lookback`` is False.

    A read of ``X[k]`` counts k bars plus what X itself reads; a windowed
    builtin (``Highest(High, Len)``) counts its length argument (by position,
    from _LENGTH_ARGS), a number or an input's default. So ``Highest(High, 10)[1]`` gives 11. Raises LookbackUnknown when
    the scan cannot bound it (a non-constant index or length, ``of N bars
    ago``, a user function, a windowed builtin whose length position is
    not known). This is an estimate from the source, not
    MultiCharts' run-time auto-detect."""
    n = _scan(el_source)
    if isinstance(n, LookbackUnknown):
        raise n
    return n


def short_history_bar(el_source, max_bars_back):
    """BarNumber of the first evaluated bar whose price lookback runs past the
    available history (and silently reads Open), or None if none does."""
    tokens, positions = tokenise(el_source)
    ast = parse(tokens, positions=positions, source=el_source)
    if max_price_lookback(ast) > max_bars_back:
        return 1
    return None


def warn_short_history(el_source, max_bars_back, n_bars, tool):
    """Print the one-per-run lookback warning on stderr. Values are unchanged."""
    if max_bars_back >= n_bars:
        return None
    bar = short_history_bar(el_source, max_bars_back)
    if bar is not None:
        print(f"[{tool}] warning: lookback past available history on bar {bar}; "
              "pass --max-bars-back to skip warm-up bars", file=sys.stderr)
    return bar


def check_max_bars_back(max_bars_back, el_source=None):
    """Validate and normalise a ``max_bars_back`` argument.

    ``"auto"`` resolves to ``infer_max_bars_back(el_source)``; when that
    cannot bound the lookback it raises ValueError rather than guess."""
    if isinstance(max_bars_back, str) and max_bars_back.strip().lower() == 'auto':
        try:
            return infer_max_bars_back(el_source)
        except LookbackUnknown as exc:
            raise ValueError(
                f"max_bars_back auto: cannot infer the lookback ({exc}); "
                "pass a number instead") from None
    mbb = int(max_bars_back or 0)
    if mbb < 0:
        raise ValueError(f"max_bars_back must be >= 0, got {max_bars_back}")
    return mbb


def check_max_entries_per_direction(max_entries_per_direction):
    """Validate and normalise a ``max_entries_per_direction`` argument.

    ``None`` or 0 means unlimited (the default); otherwise it must be >= 1.
    """
    n = int(max_entries_per_direction or 0)
    if n < 0:
        raise ValueError("max_entries_per_direction must be >= 1 (or None/0 for "
                         f"unlimited), got {max_entries_per_direction}")
    return n
