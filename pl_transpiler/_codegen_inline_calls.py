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


"""Inline Python expansions of builtin calls, keyed by BUILTIN_FUNC_MAP target.

``CodeGen._gen_call_impl`` looks the call's Python name up in
``INLINE_CALLS``. Each entry takes the EL name and the generated argument
strings and returns the expansion, or None when the arity has no faithful
expansion (the call then takes the generic / unimplemented path).
"""

from typing import Callable, Optional

InlineCall = Callable[[str, list[str]], Optional[str]]

# Expansions that call into `math` / need the f32 rounding helpers imported.
INLINE_NEEDS_MATH = frozenset(('__ceiling__', '__floor__', '__expvalue__'))
INLINE_NEEDS_F32 = frozenset(('__round__',))


def _join(args: list[str]) -> str:
    return ', '.join(args)


def _hlc(fn: str) -> InlineCall:
    """Technical indicator with high/low/close injected before one Length arg."""
    return lambda _n, a: f"{fn}(high, low, close, {a[0]})" if len(a) == 1 else None


def _plot_attr(attr: str) -> InlineCall:
    """SetPlotX(plot, value) -> _plots['plot<n>_<attr>'] = value."""
    return lambda _n, a: (f"_plots.__setitem__(f'plot{{{a[0]}}}_{attr}', {a[1]})"
                          if len(a) == 2 else None)


def _kwargs_preferred(py_name: str, key: str) -> InlineCall:
    """Let the runner override the runtime value via a per-bar kwarg."""
    return lambda _n, a: f"kwargs.get('{key}', {py_name}({_join(a)}))"


def _round(_n: str, a: list[str]) -> Optional[str]:
    if len(a) == 1:
        return f"el_round0({a[0]})"
    if len(a) == 2:
        return f"el_round({a[0]}, {a[1]})"
    return None


def _eldatetodatetime(_n: str, a: list[str]) -> Optional[str]:
    if len(a) == 1:
        # EL ELDateToDateTime(ELDate): convert an EL date (YYYMMDD,
        # years-since-1900) to a MC DateTime serial (days since 1899-12-30,
        # Excel-style). Identical day-serial base as DateToJulian.
        return f"pl_datetojulian({a[0]})"
    if len(a) == 2:
        return f"({a[0]} * 10000 + {a[1]})"
    return None


def _period_data(name: str, a: list[str]) -> str:
    """Period data functions (OpenD, HighD, etc.) read a per-bar kwarg."""
    offset = int(a[0]) if a else 0
    return f"kwargs.get('{name}_{offset}', 0)"


INLINE_CALLS: dict[str, InlineCall] = {
    # Simple math functions
    '__intportion__': lambda _n, a: f"int({a[0]})" if len(a) == 1 else None,
    '__fracportion__': lambda _n, a: f"({a[0]} - int({a[0]}))" if len(a) == 1 else None,
    '__sign__': lambda _n, a: (f"(1 if {a[0]} > 0 else (-1 if {a[0]} < 0 else 0))"
                               if len(a) == 1 else None),
    '__square__': lambda _n, a: f"({a[0]} ** 2)" if len(a) == 1 else None,
    '__numtostr__': lambda _n, a: f'f"{{{a[0]}:.{{{a[1]}}}f}}"' if len(a) >= 2 else None,
    '__strtonum__': lambda _n, a: f"float({a[0]})" if len(a) == 1 else None,

    # Technical indicators that need high/low/close injected
    '__cci__': _hlc('pl_cci'),
    '__atr__': _hlc('pl_atr'),
    '__adx__': _hlc('pl_adx'),
    '__adxr__': _hlc('pl_adxr'),
    '__dmiplus__': _hlc('pl_dmiplus'),
    '__dmiminus__': _hlc('pl_dmiminus'),
    # CountIf: count where condition is true over window
    '__countif__': lambda _n, a: f"pl_countif_window(close, open, {a[1]})",
    # Stochastic(High, Low, Close, Length, SmoothK, SmoothD, SmoothType, oFastK,
    # oFastD, oSlowK, oSlowD): the output vars are set by the function.
    '__stochastic__': lambda _n, a: f"pl_stochastic({_join(a)})",
    '__fastk__': _hlc('pl_fast_k'),
    '__fastd__': lambda _n, a: (f"pl_fast_d(high, low, close, {a[0]}, {a[1]})"
                                if len(a) == 2 else None),
    '__slowk__': lambda _n, a: (f"pl_slow_k(high, low, close, {a[0]}, {a[1]})"
                                if len(a) == 2 else None),
    '__slowd__': lambda _n, a: (f"pl_slow_d(high, low, close, {a[0]}, {a[1]}, {a[2]})"
                                if len(a) == 3 else None),

    # Math functions
    '__ceiling__': lambda _n, a: f"math.ceil({a[0]})" if len(a) == 1 else None,
    '__floor__': lambda _n, a: f"math.floor({a[0]})" if len(a) == 1 else None,
    '__round__': _round,
    '__expvalue__': lambda _n, a: f"math.exp({a[0]})" if len(a) == 1 else None,

    # String functions
    '__leftstr__': lambda _n, a: f"{a[0]}[:{a[1]}]" if len(a) == 2 else None,
    '__rightstr__': lambda _n, a: f"{a[0]}[-{a[1]}:]" if len(a) == 2 else None,
    # EL is 1-indexed: MidStr("abcdef",2,3)="bcd" -> Python "abcdef"[1:4]
    '__midstr__': lambda _n, a: (f"{a[0]}[({a[1]} - 1):({a[1]} - 1) + {a[2]}]"
                                 if len(a) == 3 else None),
    '__strlen__': lambda _n, a: f"__builtin_len({a[0]})" if len(a) == 1 else None,
    '__strcontains__': lambda _n, a: f"({a[1]} in {a[0]})" if len(a) == 2 else None,
    '__strreplace__': lambda _n, a: f"{a[0]}.replace({a[1]}, {a[2]})" if len(a) == 3 else None,
    '__strfind__': lambda _n, a: f"{a[0]}.find({a[1]})" if len(a) == 2 else None,
    '__lowerstr__': lambda _n, a: f"{a[0]}.lower()" if len(a) == 1 else None,
    '__upperstr__': lambda _n, a: f"{a[0]}.upper()" if len(a) == 1 else None,
    '__stringformat__': lambda _n, a: f"{a[0]}.format({_join(a[1:])})" if len(a) >= 1 else None,

    # Date/time functions
    '__hour__': lambda _n, a: f"({a[0]} // 100)" if len(a) == 1 else None,
    '__minute__': lambda _n, a: f"({a[0]} % 100)" if len(a) == 1 else None,
    '__second__': lambda _n, a: f"({a[0]} % 100)" if len(a) == 1 else None,

    # Output functions
    '__plotpb__': lambda _n, a: (
        f"_plots.setdefault('plotpb', []).append(({a[0]}, {a[1]}, {a[2]}, {a[3]}))"
        if len(a) == 4 else None),
    '__printlog__': lambda _n, a: f"pl_print({_join(a)})",
    '__setplotcolor__': _plot_attr('color'),
    '__setplotwidth__': _plot_attr('width'),
    '__setplotstyle__': _plot_attr('style'),

    # MC extensions
    '__basedatanumber__': lambda _n, a: "kwargs.get('base_data_number', 1)",
    '__currentdatanumber__': lambda _n, a: "kwargs.get('current_data_number', 1)",
    '__recalculate__': lambda _n, a: "kwargs.__setitem__('_needs_recalc', True)",
    '__setstopposition__': lambda _n, a: (f"_risk.__setitem__('stop_position', {a[0]})"
                                          if len(a) == 1 else None),
    '__barnumberofdata__': lambda _n, a: (f"kwargs.get('bar_number_of_data_{{{a[0]}}}', 0)"
                                          if len(a) == 1 else None),

    # Date/time no-arg functions
    '__currentdate__': lambda _n, a: "kwargs.get('current_date', 0)",
    '__currenttime__': lambda _n, a: "kwargs.get('current_time', 0)",
    '__timetostring__': lambda _n, a: f"pl_timetostring({a[0]})" if len(a) == 1 else None,
    '__datetostring__': lambda _n, a: f"pl_datetostring({a[0]})" if len(a) == 1 else None,
    '__eldatetodatetime__': _eldatetodatetime,
    # ELTimeToDateTime(HHmm) -> time-of-day fraction (pdf:4499
    # ELTimeToDateTime(1015)=0.42708333).
    '__eltimetodatetime__': lambda _n, a: (
        f"(((int({a[0]}) // 100) * 3600 + (int({a[0]}) % 100) * 60) / 86400.0)"
        if len(a) == 1 else None),
    '__datetimetoeldate__': lambda _n, a: f"int({a[0]} // 10000)" if len(a) == 1 else None,
    '__datetimetoeltime__': lambda _n, a: f"int({a[0]} % 10000)" if len(a) == 1 else None,

    '__period_data__': _period_data,

    # Output extensions
    '__setplotbgcolor__': _plot_attr('bgcolor'),
    '__noplot__': lambda _n, a: (f"_plots.__setitem__(f'plot{{{a[0]}}}_noplot', True)"
                                 if len(a) == 1 else None),

    # Order extensions
    '__setbreakeven__': lambda _n, a: (f"_risk.__setitem__('breakeven', {a[0]})"
                                       if len(a) == 1 else None),
    '__entryname__': lambda _n, a: "kwargs.get('entry_name', '')" if len(a) >= 1 else None,
    '__exitname__': lambda _n, a: "kwargs.get('exit_name', '')" if len(a) >= 1 else None,

    # CurrentSession: allow runner to override via kwarg (runner computes from bar date/time).
    'pl_currentsession': _kwargs_preferred('pl_currentsession', 'currentsession'),
    # SessionLastBar: runner derives "last bar of the trading session" from the bar
    # date series (next bar is a new day) and supplies it via 'session_last_bar',
    # mirroring CurrentSession. Not capture-fed — derived from the date series.
    'pl_sessionlastbar': _kwargs_preferred('pl_sessionlastbar', 'session_last_bar'),
    # LastCalc*: date/time of the FINAL bar of the data series (the moment of the
    # last calculation in a historical export). The runner derives these from the
    # last bar's date/time series (fundamental inputs, NOT the lc* output columns),
    # mirroring CurrentSession/SessionLastBar. Constant across all bars.
    'pl_lastcalcdatetime': _kwargs_preferred('pl_lastcalcdatetime', 'lastcalcdatetime'),
    'pl_lastcalcjdate': _kwargs_preferred('pl_lastcalcjdate', 'lastcalcjdate'),
    'pl_lastcalcmmtime': _kwargs_preferred('pl_lastcalcmmtime', 'lastcalcmmtime'),
    'pl_lastcalcmstime': _kwargs_preferred('pl_lastcalcmstime', 'lastcalcmstime'),
    'pl_lastcalcsstime': _kwargs_preferred('pl_lastcalcsstime', 'lastcalcsstime'),
}
