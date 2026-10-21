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
