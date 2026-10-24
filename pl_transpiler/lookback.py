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
from collections.abc import Callable, Iterable
from typing import Any, Optional, Union

from pl_transpiler import catalog
from pl_transpiler._types import Node
from pl_transpiler.codegen_tables import BUILTIN_FUNC_MAP
from pl_transpiler.parser import parse_source, tokenise_source

# Price series whose short-history reads fall back to the current bar's Open
# (codegen_expr._gen_bar_ref; the native interpreter's history_or_open).
_PRICE_SERIES = frozenset(
    ('close', 'high', 'low', 'open', 'volume', 'ticks', 'upticks', 'downticks'))


def max_price_lookback(ast: Node) -> int:
    """Largest constant ``n`` in a ``Close[n]``-style read of a price series.

    Only literal indexes are counted; ``Close[Length]`` is not resolved.
    """
    best = 0
    stack: list[Any] = [ast]
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


def _windowed_calls() -> frozenset[str]:
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
# ``--max-bars-back auto`` rather than guessed. Each count is checked against
# the runtime (tests/test_max_bars_back_auto.py): at that MaxBarsBack the first
# evaluated bar already has a full window. _Scan.window adds what the table
# cannot say: LinearRegValue's TgtBar, and ADX / ADXR, whose Wilder smoothing
# returns 0 and then 50 until 2 * Length (ADXR: 3 * Length - 1) bars.
_LENGTH_ARGS: dict[str, Union[int, tuple[tuple[int, ...], ...]]] = {
    **dict.fromkeys((
        'average', 'averagefc', 'averagetfc', 'bollingerband', 'countif',
        'highest', 'highestbar', 'linearregangle',
        'linearregslope', 'linearregvalue', 'lowest', 'lowestbar',
        'momentum', 'rateofchange', 'roc', 'rsi', 'standarddev',
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

# MultiCharts functions PowerBridge does not run (left out of the table):
# refused by name rather than called user functions.
_NOT_IMPLEMENTED = frozenset(('keltnerchannel', 'median', 'mro', 'parabolicsar', 'tsi'))

# Bare reserved words that read the previous bar (TrueRange uses Close[1]).
_LOOKBACK_IDENTS = frozenset(('truerange', 'truehigh', 'truelow'))

# Windowed calls whose ``Name(Price, 1)`` reads only the current bar
# (Momentum/RSI/ROC with length 1 still read Price[1]).
_CURRENT_BAR_AT_1 = frozenset((
    'average', 'averagefc', 'averagetfc', 'highest', 'highestbar', 'lowest',
    'lowestbar', 'summation', 'waverage', 'weightedaverage', 'xaverage'))


WARMUP_NOTE = ("note: {tool}: strategy reads past bars and the default "
               "--max-bars-back auto cannot find the lookback, so it is 0; "
               "fills may differ from MultiCharts during warm-up "
               "(set --max-bars-back N, or -q to hide this)")


def _base_name(name: str) -> str:
    """``name`` with a trailing ``FC`` dropped when that names a windowed call."""
    if name not in _WINDOWED_CALLS and name not in _LENGTH_ARGS and name.endswith('fc'):
        return name[:-2]
    return name


def _is_windowed(name: str) -> bool:
    name = _base_name(name)
    return name in _WINDOWED_CALLS or name in _LENGTH_ARGS or name in _NOT_IMPLEMENTED


def _is_number(node: Any, pred: Callable[[float], bool]) -> bool:
    if not (isinstance(node, dict) and node.get('type') == 'number'):
        return False
    try:
        return pred(float(node.get('value') or 0))
    except (TypeError, ValueError):
        return False


def _bars_ago(tokens: Iterable[tuple[Any, Any]]) -> bool:
    """True for the ``X of N bar(s) ago`` form, N > 0 (kept out of the AST)."""
    words = [(kind, str(text).lower()) for kind, text in tokens]
    for i in range(len(words) - 3):
        if (words[i] == ('IDENT', 'of') and words[i + 1][0] == 'NUMBER'
                and words[i + 2][1] in ('bar', 'bars') and words[i + 3][1] == 'ago'
                and float(words[i + 1][1]) != 0):
            return True
    return False


def uses_lookback(el_source: str) -> bool:
    """True if the strategy may read past bars: it indexes a series (``x[n]``
    with n not a literal 0, or ``x of n bars ago``), uses TrueRange, calls a
    windowed builtin (Average, Highest, an ``*FC`` variant, ...) with a length
    other than a literal 1, or calls a user function (which may index
    internally). The same scan as ``infer_max_bars_back``: True when that
    finds a lookback or cannot bound one."""
    return _scan(el_source) != 0


def note_warmup(el_source: str, max_bars_back: int, tool: str, quiet: bool = False,
                n_bars: Optional[int] = None) -> bool:
    """CLI only: print the once-per-run warm-up note on stderr when the
    strategy uses lookbacks and MaxBarsBack is 0. Returns True if printed.
    The tools call it only when the default auto could not find the lookback.

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
    print(WARMUP_NOTE.format(tool=tool), file=sys.stderr)
    return True


class LookbackUnknown(ValueError):
    """``infer_max_bars_back`` could not bound the strategy's lookback."""


def _input_defaults(ast: Node) -> dict[str, float]:
    """Lower-cased input name -> numeric literal default."""
    out = {}
    for stmt in ast.get('body') or []:
        if isinstance(stmt, dict) and stmt.get('type') == 'input_decl':
            for decl in stmt.get('decls') or []:
                default = decl.get('default')
                if isinstance(default, dict) and default.get('type') == 'number':
                    out[str(decl.get('name', '')).lower()] = float(default['value'])
    return out


def _const(arg: Any, inputs: dict[str, float]) -> Optional[float]:
    """A number, an input's default, or either negated; else None."""
    if isinstance(arg, dict) and arg.get('type') == 'unaryop' and arg.get('op') == '-':
        value = _const(arg.get('operand'), inputs)
        return None if value is None else -value
    if _is_number(arg, lambda v: True):
        return float(arg['value'])
    if isinstance(arg, dict) and arg.get('type') == 'ident':
        return inputs.get(str(arg.get('name', '')).lower())
    return None


class _Scan:
    """One walk of a parsed strategy; see infer_max_bars_back."""

    def __init__(self, ast: Node, source: str) -> None:
        self.inputs = _input_defaults(ast)
        self.lines = source.splitlines()
        self.line = None
        # Variable -> past price bars its assignments read (0 if none).
        self.var_need: dict[str, int] = {}
        self.vars_pass = True
        # Declared arrays (anywhere): ``Arr[i]`` parses to a bar_ref but is an
        # element read, not a past-bar read.
        self.arrays: set[str] = set()
        assigns = []
        stack: list[Any] = [ast]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get('type') == 'array_decl':
                    self.arrays.update(str(d.get('name', '')).lower()
                                       for d in node.get('decls') or [])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        stack = [ast]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                target = self.assigned(node)
                if target:
                    assigns.append((target, node))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        # Source order, so a refusal names the first offending line.
        assigns.sort(key=lambda item: item[1].get('_line') or 0)
        # A variable read without [n] counts what it reads; repeat until the
        # counts settle (a self-referencing variable stops after a bounded
        # number of rounds and is refused in the second pass).
        for _ in range(len(assigns) + 2):
            changed = False
            for name, node in assigns:
                self.line = node.get('_line')
                n = self.need(node.get('value'))
                if n > self.var_need.get(name, 0):
                    self.var_need[name] = n
                    changed = True
            if not changed:
                break
        self.vars_pass = False

    def array_series(self, node: Any) -> Optional[str]:
        """The lower-cased array name when ``node`` is an ``Arr[i]`` element."""
        if isinstance(node, dict) and node.get('type') == 'bar_ref':
            series = node.get('series')
            if isinstance(series, dict) and series.get('type') == 'ident':
                name = str(series.get('name', '')).lower()
                if name in self.arrays:
                    return name
        return None

    def assigned(self, node: Any) -> Optional[str]:
        """The lower-cased variable or array an ``assign`` node writes, else None."""
        if not (isinstance(node, dict) and node.get('type') == 'assign'):
            return None
        target = node.get('target')
        if isinstance(target, dict) and target.get('type') == 'ident':
            return str(target.get('name', '')).lower()
        return self.array_series(target)

    def name(self, node: Node) -> str:
        """The identifier as written in the source (``FastD``, ``File``)."""
        line, col = node.get('_line'), node.get('_col')
        text = str(node.get('_ident') or node.get('name', ''))
        if isinstance(line, int) and isinstance(col, int) and 0 < line <= len(self.lines):
            word = self.lines[line - 1][col - 1:col - 1 + len(text)]
            if word.lower() == text.lower():
                return word
        return text

    def unknown(self, what: str) -> LookbackUnknown:
        where = f" on line {self.line}" if self.line else ""
        return LookbackUnknown(f"{what}{where}")

    def index(self, node: Any, what: str) -> int:
        """A non-negative whole [n] or length: a number or an input's default."""
        value = _const(node, self.inputs)
        if value is None or value < 0 or value != int(value):
            raise self.unknown(what)
        return int(value)

    def window(self, node: Node, args: list[Any]) -> int:
        """Past bars a windowed builtin call reads, from _LENGTH_ARGS."""
        label = self.name(node)
        name = _base_name(str(node.get('name', '')).lower())
        if name in _NOT_IMPLEMENTED:
            raise self.unknown(f"{label}() is not implemented in PowerBridge")
        spec = _LENGTH_ARGS.get(name)
        if isinstance(spec, int):
            return spec
        if spec is None or max(p for group in spec for p in group) >= len(args):
            raise self.unknown(f"the length argument of {label}() is not known")
        what = f"the length of {label}() is not a number or input"
        lengths = [[self.index(args[p], what) for p in group] for group in spec]
        if name in _CURRENT_BAR_AT_1 and lengths[0][0] <= 1:
            return 0
        n = sum(max(group) for group in lengths)
        if name == 'linearregvalue' and len(args) > 2:
            # TgtBar > 0 reads that many bars further back (_linreg_window).
            tgt = _const(args[2], self.inputs)
            if tgt is None:
                raise self.unknown(f"the TgtBar of {label}() is not a number or input")
            n += max(int(tgt), 0)
        if name == 'adx':
            n *= 2  # Wilder-smoothed DX: 0 then 50 until 2 * Length bars
        elif name == 'adxr':
            n = 3 * n - 1  # ADX now and Length - 1 bars ago
        return n

    def var_in(self, node: Any) -> Optional[str]:
        """The source name of a variable that reads past bars, when ``node``
        is just that variable; else None."""
        if (isinstance(node, dict) and node.get('type') == 'ident'
                and self.var_need.get(str(node.get('name', '')).lower())):
            return self.name(node)
        return None

    def need(self, node: Any) -> int:
        """Past bars an expression reads (see infer_max_bars_back)."""
        if isinstance(node, list):
            return max((self.need(n) for n in node), default=0)
        if not isinstance(node, dict):
            return 0
        if isinstance(node.get('_line'), int):
            self.line = node['_line']
        kind = node.get('type')
        name = str(node.get('name', '')).lower()
        if kind == 'assign':
            # The target is written, not read; an array target's index is read.
            target: Any = node.get('target')
            n = self.need(node.get('value'))
            if self.array_series(target):
                n = max(n, self.need(target.get('index')))
            return n
        if kind == 'bar_ref' and self.array_series(node):
            # An element read: what the array holds plus what its index reads.
            inner = self.need(node.get('index'))
            held = self.var_need.get(self.array_series(node) or '', 0)
            return max(inner, held if self.vars_pass else 0)
        if kind == 'bar_ref':
            series = node.get('series')
            k = self.index(node.get('index'),
                           "a series is indexed by a non-constant [n]")
            inner = self.need(series)
            if isinstance(series, dict) and series.get('type') == 'ident':
                var = str(series.get('name', '')).lower()
                if var not in _PRICE_SERIES:
                    if not self.var_need.get(var):
                        # Its own history only: no price bars (first pass).
                        return 0 if self.vars_pass else k
                    if k and not self.vars_pass:
                        label = self.name(series)
                        raise self.unknown(
                            f"the variable {label} reads past bars and is itself "
                            f"read {k} bar{'s' if k > 1 else ''} back ({label}[{k}])")
            return k + inner
        if kind == 'call':
            args = node.get('args') or []
            inner = self.need(args)
            if name in ('crossesabove', 'crossesbelow', 'crossover', 'crossunder') and len(args) == 2:
                return 1 + inner
            if _is_windowed(name):
                n = self.window(node, args)
                if n and not self.vars_pass:
                    for arg in args:
                        var_name = self.var_in(arg)
                        if var_name:
                            raise self.unknown(
                                f"the variable {var_name} reads past bars and "
                                f"{self.name(node)}() reads its past values")
                return n + inner
            if not _is_builtin(name):
                raise self.unknown(f"it calls {self.name(node)}(), which is a user "
                                  f"function or a keyword PowerBridge does not run")
            return inner
        if kind == 'ident':
            if name in _LOOKBACK_IDENTS:
                return 1
            return self.var_need.get(name, 0) if self.vars_pass else 0
        return self.need([v for k, v in node.items() if not str(k).startswith('_')])


def _is_builtin(name: str) -> bool:
    """A call codegen handles itself (BUILTIN_FUNC_MAP, a runtime ``pl_<name>``
    such as File or FileAppend, or a catalog keyword), not a user function."""
    from pl_transpiler.codegen_util import available_pl_targets
    return (name in BUILTIN_FUNC_MAP or f'pl_{name}' in available_pl_targets()
            or catalog.get(name) is not None)


def _scan(el_source: str) -> Union[int, LookbackUnknown]:
    """The one lookback scan behind the warm-up note and ``auto``: the
    longest lookback (an int, 0 if none) or the LookbackUnknown raised when
    it cannot be bounded."""
    tokens, _ = tokenise_source(el_source)
    if _bars_ago(tokens):
        return LookbackUnknown("it uses 'of N bars ago'")
    ast = parse_source(el_source)
    try:
        scan = _Scan(ast, el_source)
        return scan.need(ast)
    except LookbackUnknown as exc:
        return exc


def infer_max_bars_back(el_source: str) -> int:
    """The MaxBarsBack that ``--max-bars-back auto`` uses: the longest
    lookback found by a static scan, or 0 if ``uses_lookback`` is False.

    A read of ``X[k]`` counts k bars plus what X itself reads; a windowed
    builtin (``Highest(High, Len)``) counts the bars the runtime needs for a
    full window of price data (_LENGTH_ARGS, by argument position). An index or length may
    be a number or an input's default, so ``Highest(High, 10)[1]`` and
    ``Inputs: Len(10); ... Close[Len]`` give 11 and 10. Raises
    LookbackUnknown, naming the construct and its line, when the scan cannot
    bound it: a non-constant index or length, ``of N bars ago``, a user
    function, a builtin PowerBridge does not run, or ``X[k]`` on a variable X
    that itself reads past bars (its history starts on the first evaluated
    bar, so no MaxBarsBack fills it). This is an estimate from the source, not
    MultiCharts' run-time auto-detect. A plain variable passed to a
    windowed builtin (``Average(Mid, 10)``) counts the window, but the
    variable's own history still starts on the first evaluated bar."""
    n = _scan(el_source)
    if isinstance(n, LookbackUnknown):
        raise n
    return n


def short_history_bar(el_source: str, max_bars_back: int) -> Optional[int]:
    """BarNumber of the first evaluated bar whose price lookback runs past the
    available history (and silently reads Open), or None if none does."""
    ast = parse_source(el_source)
    if max_price_lookback(ast) > max_bars_back:
        return 1
    return None


def warn_short_history(el_source: str, max_bars_back: int, n_bars: int, tool: str) -> Optional[int]:
    """Print the one-per-run lookback warning on stderr. Values are unchanged."""
    if max_bars_back >= n_bars:
        return None
    bar = short_history_bar(el_source, max_bars_back)
    if bar is not None:
        print(f"[{tool}] warning: lookback past available history on bar {bar}; "
              "pass --max-bars-back to skip warm-up bars", file=sys.stderr)
    return bar


def check_max_bars_back(max_bars_back: Union[int, str, None], el_source: Optional[str] = None) -> int:
    """Validate and normalise a ``max_bars_back`` argument.

    ``"auto"`` resolves to ``infer_max_bars_back(el_source)``; when that
    cannot bound the lookback it raises ValueError rather than guess.
    ``None`` (the default) is auto too, but falls back to 0 when the lookback
    cannot be inferred."""
    if max_bars_back is None:
        try:
            return infer_max_bars_back(el_source) if el_source is not None else 0
        except LookbackUnknown:
            return 0
    if isinstance(max_bars_back, str) and max_bars_back.strip().lower() == 'auto':
        try:
            return infer_max_bars_back(el_source)  # type: ignore[arg-type]  # auto needs the source
        except LookbackUnknown as exc:
            raise ValueError(
                f"max_bars_back auto: cannot infer the lookback ({exc}); "
                "pass a number instead") from None
    mbb = int(max_bars_back or 0)
    if mbb < 0:
        raise ValueError(f"max_bars_back must be >= 0, got {max_bars_back}")
    return mbb


def check_max_entries_per_direction(max_entries_per_direction: Optional[int]) -> int:
    """Validate and normalise a ``max_entries_per_direction`` argument.

    ``None`` or 0 means unlimited (the default); otherwise it must be >= 1.
    """
    n = int(max_entries_per_direction or 0)
    if n < 0:
        raise ValueError("max_entries_per_direction must be >= 1 (or None/0 for "
                         f"unlimited), got {max_entries_per_direction}")
    return n
