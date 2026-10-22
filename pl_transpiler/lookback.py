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


def _is_windowed(name):
    return name in _WINDOWED_CALLS or (
        name.endswith('fc') and name[:-2] in _WINDOWED_CALLS)


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
    internally)."""
    tokens, positions = tokenise(el_source)
    if _bars_ago(tokens):
        return True
    stack = [parse(tokens, positions=positions, source=el_source)]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            kind = node.get('type')
            name = str(node.get('name', '')).lower()
            if kind == 'call':
                args = node.get('args') or []
                if _is_windowed(name):
                    if not (name in _CURRENT_BAR_AT_1 and len(args) == 2
                            and _is_number(args[1], lambda v: v <= 1)):
                        return True
                elif name not in BUILTIN_FUNC_MAP and catalog.get(name) is None:
                    return True
            elif kind == 'ident' and name in _LOOKBACK_IDENTS:
                return True
            elif kind == 'bar_ref' and not _is_number(node.get('index'), lambda v: v == 0):
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def note_warmup(el_source, max_bars_back, tool, quiet=False, n_bars=None):
    """CLI only: print the once-per-run warm-up note on stderr when the
    strategy uses lookbacks and MaxBarsBack is 0. Returns True if printed.

    Skipped when the run will print the short-history warning (same cause),
    so one run never prints both. ``quiet`` hides only this note."""
    if quiet or max_bars_back or not uses_lookback(el_source):
        return False
    if (n_bars is not None and max_bars_back < n_bars
            and short_history_bar(el_source, max_bars_back) is not None):
        return False
    print(WARMUP_NOTE.format(tool=tool), file=sys.stderr)
    return True


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


def check_max_bars_back(max_bars_back):
    """Validate and normalise a ``max_bars_back`` argument."""
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
