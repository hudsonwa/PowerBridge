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

"""PowerLanguage Code Generator — walks AST and emits Python source."""

import math

# Re-exported so existing `from pl_transpiler.codegen import UnimplementedKeywordError`
# imports keep working; the class itself now lives in pl_transpiler.errors.
from .errors import UnimplementedKeywordError


_PL_TARGETS_CACHE = None


def _available_pl_targets():
    """Set of every callable pl_* name resolvable in pl_transpiler.builtins (which
    auto-imports every pl_* callable from the runtime). Cached after first use."""
    global _PL_TARGETS_CACHE
    if _PL_TARGETS_CACHE is None:
        from . import builtins as _b
        _PL_TARGETS_CACHE = {
            n for n in dir(_b)
            if n.startswith('pl_') and callable(getattr(_b, n))
        }
    return _PL_TARGETS_CACHE


_RUNTIME_BARE_CACHE = None


def _runtime_bound_bare_names():
    """Bare (lowercased) identifiers that resolve at RUNTIME when emitted verbatim
    into the generated strategy() scope: the function params, the wrapper
    prologue's own locals, every public name pulled in by
    `from pl_transpiler.runtime.pl_runtime import *`, and the Python builtins.
    A bare identifier reaching gen_expr's ident fallback is emitted raw; that is
    only safe when the name is in THIS set (or a generated local). A reserved word
    the parser 'knows' but which binds to nothing here (bare `Pi`, a pl_-only
    keyword, an order-syntax word mis-used as a value) would emit an undefined
    name -> silent NameError, zeroing the trace — so it must fail loud (BUG D)."""
    global _RUNTIME_BARE_CACHE
    if _RUNTIME_BARE_CACHE is None:
        from .runtime import pl_runtime as _rt
        names = {n for n in dir(_rt) if not n.startswith('_')}
        # FAIL-CLOSED (a2 audit F1): do NOT trust the whole `dir(builtins)` set as a
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
            '_trace',
        }
        _RUNTIME_BARE_CACHE = names
    return _RUNTIME_BARE_CACHE

# Version of the clean (trace=False) Python dialect this codegen emits. R4's
# transpiler/py_front.py asserts a matching constant at import: a drift tripwire
# so that changing the emitted dialect forces a deliberate bump in BOTH files.
# Constant only — no behavioural effect. Bumped 1->2 for Rcmt: clean (trace=False)
# output now carries the source EL comments as `#| <verbatim>` lines so they survive
# the EL->Py->EL round trip (py_front recaptures them). Comments are inert (Python
# comment lines) — no effect on execution or on the traced/dumped values.
# Bumped 2->3 for the Rcyc anchor fix: a comment anchored to the SAME source line as
# its statement (a trailing `{...}`) is now emitted as a same-line `#| <verbatim>`
# suffix (was its own line after the statement), so py_front can tell a trailing
# comment from a leading one and re-anchor it to the identical statement. Still inert.
DIALECT_VERSION = 3

# Built-in function name mapping (PL lowercase → Python)
BUILTIN_FUNC_MAP = {
    'average': 'pl_average',
    'averagefc': 'pl_average',
    'highest': 'pl_highest',
    'lowest': 'pl_lowest',
    'crossesabove': 'pl_crosses_above',
    'crossesbelow': 'pl_crosses_below',
    'rsi': 'pl_rsi',
    'momentum': 'pl_momentum',
    'summation': 'pl_summation',
    'stddev': 'pl_std_dev',
    'truerange': 'pl_true_range',
    'absvalue': 'abs',
    'maxlist': 'max',
    'minlist': 'min',
    'log': 'math.log',
    'sqrt': 'math.sqrt',
    'standarddev': 'pl_std_dev',
    'intportion': '__intportion__',
    'fracportion': '__fracportion__',
    'sign': '__sign__',
    'square': '__square__',
    'numtostr': '__numtostr__',
    'strtonum': '__strtonum__',
    # Technical indicators
    'macd': 'pl_macd',
    'macdvalue': 'pl_macd',
    'macddiff': 'pl_macd_diff',
    'macdsignal': 'pl_macd_signal',
    'roc': 'pl_roc',
    'rateofchange': 'pl_roc',
    'cci': '__cci__',
    'atr': '__atr__',
    'avgtruerange': '__atr__',
    'fastk': '__fastk__',
    'fastd': '__fastd__',
    'slowk': '__slowk__',
    'slowd': '__slowd__',
    'linearregvalue': 'pl_linear_reg_value',
    'linearregslope': 'pl_linear_reg_slope',
    'variance': 'pl_variance',
    # Math functions
    'ceiling': '__ceiling__',
    'floor': '__floor__',
    'round': '__round__',
    'expvalue': '__expvalue__',
    # String functions
    'leftstr': '__leftstr__',
    'rightstr': '__rightstr__',
    'midstr': '__midstr__',
    'strlen': '__strlen__',
    'strcontains': '__strcontains__',
    'strreplace': '__strreplace__',
    'strfind': '__strfind__',
    'lowerstr': '__lowerstr__',
    'upperstr': '__upperstr__',
    'stringformat': '__stringformat__',
    # Date/time functions
    'hour': '__hour__',
    'minute': '__minute__',
    'second': '__second__',
    # Drawing functions
    'tl_new': 'pl_tl_new',
    'tl_setcolor': 'pl_tl_setcolor',
    'tl_setstyle': 'pl_tl_setstyle',
    'tl_setwidth': 'pl_tl_setwidth',
    'tl_delete': 'pl_tl_delete',
    'text_new': 'pl_text_new',
    'text_setstring': 'pl_text_setstring',
    'text_setcolor': 'pl_text_setcolor',
    'text_delete': 'pl_text_delete',
    'arw_new': 'pl_arw_new',
    # MC extensions
    'basedatanumber': '__basedatanumber__',
    'currentdatanumber': '__currentdatanumber__',
    # Instrument / chart-config reserved words (used bare, no parens)
    'category': 'pl_category',
    'description': 'pl_description',
    'intervaltype': 'pl_intervaltype',
    'intervaltype_ex': 'pl_intervaltype_ex',
    'recalculate': '__recalculate__',
    # Output
    'plotpb': '__plotpb__',
    'printlog': '__printlog__',
    'setplotcolor': '__setplotcolor__',
    'setplotwidth': '__setplotwidth__',
    'setplotstyle': '__setplotstyle__',
    'setstopposition': '__setstopposition__',
    # Additional functions
    'averagetfc': 'pl_average',
    'weightedaverage': 'pl_weighted_average',
    'waverage': 'pl_weighted_average',
    'xaverage': 'pl_xaverage',
    'highestbar': 'pl_highest_bar',
    'lowestbar': 'pl_lowest_bar',
    'correlation': 'pl_correlation',
    'stochk': '__fastk__',
    'stochd': '__slowd__',
    'currentdate': '__currentdate__',
    'currenttime': '__currenttime__',
    'timetostring': '__timetostring__',
    'datetostring': '__datetostring__',
    'eldatetodatetime': '__eldatetodatetime__',
    'eltimetodatetime': '__eltimetodatetime__',
    'datetimetoeldate': '__datetimetoeldate__',
    'datetimetoeltime': '__datetimetoeltime__',
    # Period data functions
    'opend': '__period_data__',
    'highd': '__period_data__',
    'lowd': '__period_data__',
    'closed': '__period_data__',
    'volumed': '__period_data__',
    'openw': '__period_data__',
    'highw': '__period_data__',
    'loww': '__period_data__',
    'closew': '__period_data__',
    'openm': '__period_data__',
    'highm': '__period_data__',
    'lowm': '__period_data__',
    'closem': '__period_data__',
    # Drawing extensions
    'tl_new_s': 'pl_tl_new_s',
    'tl_setbegin': 'pl_tl_setbegin',
    'tl_setend': 'pl_tl_setend',
    'tl_setextleft': 'pl_tl_setextleft',
    'tl_setextright': 'pl_tl_setextright',
    'tl_getbeginbar': 'pl_tl_getbeginbar',
    'tl_getendbar': 'pl_tl_getendbar',
    'tl_getbeginval': 'pl_tl_getbeginval',
    'tl_getendval': 'pl_tl_getendval',
    'text_new_s': 'pl_text_new_s',
    'text_setstyle': 'pl_text_setstyle',
    'text_setlocation': 'pl_text_setlocation',
    'text_getstring': 'pl_text_getstring',
    'text_setfontname': 'pl_text_setfontname',
    'text_setfontsize': 'pl_text_setfontsize',
    'arw_new_s': 'pl_arw_new_s',
    # Array functions
    'array_setmaxindex': 'pl_array_setmaxindex',
    'array_getmaxindex': 'pl_array_getmaxindex',
    'array_sum': 'pl_array_sum',
    'array_highest': 'pl_array_highest',
    'array_lowest': 'pl_array_lowest',
    'variancearray': 'pl_variancearray',
    'sortarray': 'pl_sortarray',
    # MC extensions
    'barnumberofdata': '__barnumberofdata__',
    # Output
    'setplotbgcolor': '__setplotbgcolor__',
    'noplot': '__noplot__',
    # Order
    'setbreakeven': '__setbreakeven__',
    'entryname': '__entryname__',
    'exitname': '__exitname__',
    # GT coverage functions
    'dayofweek': 'pl_dayofweek',
    'month': 'pl_month',
    'year': 'pl_year',
    'dayofmonth': 'pl_dayofmonth',
    'calcdate': 'pl_calcdate',
    'calctime': 'pl_calctime',
    'iff': 'pl_iff',
    'countif': '__countif__',
    'linearregangle': 'pl_linear_reg_angle',
    'adx': '__adx__',
    'adxr': '__adxr__',
    'dmiplus': '__dmiplus__',
    'dmiminus': '__dmiminus__',
    'bollingerband': 'pl_bollinger_band',
    'stochastic': '__stochastic__',
    # ---- Auto-generated function mappings ----
    'allowsendordersalways': 'pl_allowsendordersalways',
    'arctangent': 'pl_arctangent',
    'array_compare': 'pl_array_compare',
    'array_contains': 'pl_array_contains',
    'array_copy': 'pl_array_copy',
    'array_getbooleanvalue': 'pl_array_getbooleanvalue',
    'array_getfloatvalue': 'pl_array_getfloatvalue',
    'array_getintegervalue': 'pl_array_getintegervalue',
    'array_getstringvalue': 'pl_array_getstringvalue',
    'array_gettype': 'pl_array_gettype',
    'array_indexof': 'pl_array_indexof',
    'array_setbooleanvalue': 'pl_array_setbooleanvalue',
    'array_setfloatvalue': 'pl_array_setfloatvalue',
    'array_setintegervalue': 'pl_array_setintegervalue',
    'array_setstringvalue': 'pl_array_setstringvalue',
    'array_setvalrange': 'pl_array_setvalrange',
    'array_sort': 'pl_array_sort',
    'arraysize': 'pl_arraysize',
    'arraystartaddr': 'pl_arraystartaddr',
    'arw_anchor_to_bars': 'pl_arw_anchor_to_bars',
    'arw_delete': 'pl_arw_delete',
    'arw_get_anchor_to_bars': 'pl_arw_get_anchor_to_bars',
    'arw_getactive': 'pl_arw_getactive',
    'arw_getbarnumber': 'pl_arw_getbarnumber',
    'arw_getcolor': 'pl_arw_getcolor',
    'arw_getdate': 'pl_arw_getdate',
    'arw_getdirection': 'pl_arw_getdirection',
    'arw_getfirst': 'pl_arw_getfirst',
    'arw_getlock': 'pl_arw_getlock',
    'arw_getnext': 'pl_arw_getnext',
    'arw_getsize': 'pl_arw_getsize',
    'arw_getstyle': 'pl_arw_getstyle',
    'arw_gettext': 'pl_arw_gettext',
    'arw_gettextattribute': 'pl_arw_gettextattribute',
    'arw_gettextbgcolor': 'pl_arw_gettextbgcolor',
    'arw_gettextcolor': 'pl_arw_gettextcolor',
    'arw_gettextfontname': 'pl_arw_gettextfontname',
    'arw_gettextsize': 'pl_arw_gettextsize',
    'arw_gettime': 'pl_arw_gettime',
    'arw_gettime_dt': 'pl_arw_gettime_dt',
    'arw_gettime_s': 'pl_arw_gettime_s',
    'arw_getval': 'pl_arw_getval',
    'arw_lock': 'pl_arw_lock',
    'arw_new_bn': 'pl_arw_new_bn',
    'arw_new_dt': 'pl_arw_new_dt',
    'arw_setbarnumber': 'pl_arw_setbarnumber',
    'arw_setcolor': 'pl_arw_setcolor',
    'arw_setlocation': 'pl_arw_setlocation',
    'arw_setlocation_bn': 'pl_arw_setlocation_bn',
    'arw_setlocation_dt': 'pl_arw_setlocation_dt',
    'arw_setlocation_s': 'pl_arw_setlocation_s',
    'arw_setsize': 'pl_arw_setsize',
    'arw_setstyle': 'pl_arw_setstyle',
    'arw_settext': 'pl_arw_settext',
    'arw_settextattribute': 'pl_arw_settextattribute',
    'arw_settextbgcolor': 'pl_arw_settextbgcolor',
    'arw_settextcolor': 'pl_arw_settextcolor',
    'arw_settextfontname': 'pl_arw_settextfontname',
    'arw_settextsize': 'pl_arw_settextsize',
    'asksize': 'pl_asksize',
    'atcommentarybar': 'pl_atcommentarybar',
    'autosession': 'pl_autosession',
    'avglist': 'pl_avglist',
    'bidsize': 'pl_bidsize',
    'bigpointvalue': 'pl_bigpointvalue',
    'boxsize': 'pl_boxsize',
    'changemarketposition': 'pl_changemarketposition',
    'checkcommentary': 'pl_checkcommentary',
    'cleardebug': 'pl_cleardebug',
    'clearprintlog': 'pl_clearprintlog',
    'commandline': 'pl_commandline',
    'commentarycl': 'pl_commentarycl',
    'commentaryenabled': 'pl_commentaryenabled',
    'commission': 'pl_commission',
    'computerdatetime': 'pl_computerdatetime',
    'convert_currency': 'pl_convert_currency',
    'cosine': 'pl_cosine',
    'cotangent': 'pl_cotangent',
    'currentopenint': 'pl_currentopenint',
    'currentsession': 'pl_currentsession',
    'currenttime_s': 'pl_currenttime_s',
    'dailyclose': 'pl_dailyclose',
    'dailyhigh': 'pl_dailyhigh',
    'dailylimit': 'pl_dailylimit',
    'dailylow': 'pl_dailylow',
    'dailyopen': 'pl_dailyopen',
    'dailyvolume': 'pl_dailyvolume',
    'datacompression': 'pl_datacompression',
    'datetime': 'pl_datetime',
    'datetime2eltime': 'pl_datetime2eltime',
    'datetime2eltime_s': 'pl_datetime2eltime_s',
    'datetime_bar_update': 'pl_datetime_bar_update',
    'datetimetostring': 'pl_datetimetostring',
    'datetimetostring_ms': 'pl_datetimetostring_ms',
    'datetojulian': 'pl_datetojulian',
    'dayfromdatetime': 'pl_dayfromdatetime',
    'dayofweekfromdatetime': 'pl_dayofweekfromdatetime',
    'dom_askprice': 'pl_dom_askprice',
    'dom_askscount': 'pl_dom_askscount',
    'dom_asksize': 'pl_dom_asksize',
    'dom_bidprice': 'pl_dom_bidprice',
    'dom_bidscount': 'pl_dom_bidscount',
    'dom_bidsize': 'pl_dom_bidsize',
    'dom_isconnected': 'pl_dom_isconnected',
    'encodedate': 'pl_encodedate',
    'encodetime': 'pl_encodetime',
    'exchlisted': 'pl_exchlisted',
    'execoffset': 'pl_execoffset',
    'expirationdate': 'pl_expirationdate',
    'expirationdatefromvendor': 'pl_expirationdatefromvendor',
    'fileappend': 'pl_fileappend',
    'filedelete': 'pl_filedelete',
    'fill_array': 'pl_fill_array',
    'formatdate': 'pl_formatdate',
    'formattime': 'pl_formattime',
    'getaccount': 'pl_getaccount',
    'getaccountid': 'pl_getaccountid',
    'getappinfo': 'pl_getappinfo',
    'getbackgroundcolor': 'pl_getbackgroundcolor',
    'getbvalue': 'pl_getbvalue',
    'getcdromdrive': 'pl_getcdromdrive',
    'getcountry': 'pl_getcountry',
    'getcurrency': 'pl_getcurrency',
    'getexchangename': 'pl_getexchangename',
    'getgvalue': 'pl_getgvalue',
    'getnumaccounts': 'pl_getnumaccounts',
    'getnumpositions': 'pl_getnumpositions',
    'getplotbgcolor': 'pl_getplotbgcolor',
    'getplotcolor': 'pl_getplotcolor',
    'getplotwidth': 'pl_getplotwidth',
    'getpositionaverageprice': 'pl_getpositionaverageprice',
    'getpositionopenpl': 'pl_getpositionopenpl',
    'getpositionquantity': 'pl_getpositionquantity',
    'getpositionsymbol': 'pl_getpositionsymbol',
    'getpositiontotalcost': 'pl_getpositiontotalcost',
    'getrtaccountequity': 'pl_getrtaccountequity',
    'getrtaccountnetworth': 'pl_getrtaccountnetworth',
    'getrtsymbolname': 'pl_getrtsymbolname',
    'getrtunrealizedpl': 'pl_getrtunrealizedpl',
    'getrvalue': 'pl_getrvalue',
    'getstrategyname': 'pl_getstrategyname',
    'getsymbolname': 'pl_getsymbolname',
    'getuserid': 'pl_getuserid',
    'getusername': 'pl_getusername',
    'gradientcolor': 'pl_gradientcolor',
    'hoursfromdatetime': 'pl_hoursfromdatetime',
    'i_closedequity': 'pl_i_closedequity',
    'i_currentcontracts': 'pl_i_currentcontracts',
    'i_currentshares': 'pl_i_currentshares',
    'i_getplotvalue': 'pl_i_getplotvalue',
    'i_openequity': 'pl_i_openequity',
    'i_setplotvalue': 'pl_i_setplotvalue',
    'incmonth': 'pl_incmonth',
    'initialcapital': 'pl_initialcapital',
    'insideask': 'pl_insideask',
    'insidebid': 'pl_insidebid',
    'instr': 'pl_instr',
    'intrabarordergeneration': 'pl_intrabarordergeneration',
    'jpy': 'pl_jpy',
    'juliantodate': 'pl_juliantodate',
    'last': 'pl_last',
    'lastcalcdatetime': 'pl_lastcalcdatetime',
    'lastcalcjdate': 'pl_lastcalcjdate',
    'lastcalcmmtime': 'pl_lastcalcmmtime',
    'lastcalcmstime': 'pl_lastcalcmstime',
    'lastcalcsstime': 'pl_lastcalcsstime',
    'legacycolortorgb': 'pl_legacycolortorgb',
    'legacycolorvalue': 'pl_legacycolorvalue',
    'lower': 'pl_lower',
    'lpbool': 'pl_lpbool',
    'lpbyte': 'pl_lpbyte',
    'lpdouble': 'pl_lpdouble',
    'lpdword': 'pl_lpdword',
    'lpfloat': 'pl_lpfloat',
    'lpint': 'pl_lpint',
    'lplong': 'pl_lplong',
    'lpstr': 'pl_lpstr',
    'lpword': 'pl_lpword',
    'margin': 'pl_margin',
    'maxbarsback': 'pl_maxbarsback',
    'maxbarsforward': 'pl_maxbarsforward',
    'maxcontractsheld': 'pl_maxcontractsheld',
    'maxiddrawdown': 'pl_maxiddrawdown',
    'maxlist2': 'pl_maxlist2',
    'maxshares': 'pl_maxshares',
    'maxsharesheld': 'pl_maxsharesheld',
    'mc_arw_getactive': 'pl_mc_arw_getactive',
    'mc_text_getactive': 'pl_mc_text_getactive',
    'mc_tl_getactive': 'pl_mc_tl_getactive',
    'mc_tl_new': 'pl_mc_tl_new',
    'mc_tl_new_bn': 'pl_mc_tl_new_bn',
    'mc_tl_new_dt': 'pl_mc_tl_new_dt',
    'mc_tl_new_self': 'pl_mc_tl_new_self',
    'mc_tl_new_self_bn': 'pl_mc_tl_new_self_bn',
    'messagelog': 'pl_messagelog',
    'millisecondsfromdatetime': 'pl_millisecondsfromdatetime',
    'minlist2': 'pl_minlist2',
    'minmove': 'pl_minmove',
    'minutesfromdatetime': 'pl_minutesfromdatetime',
    'monthfromdatetime': 'pl_monthfromdatetime',
    'mouseclickbarnumber': 'pl_mouseclickbarnumber',
    'mouseclickctrlpressed': 'pl_mouseclickctrlpressed',
    'mouseclickdatanumber': 'pl_mouseclickdatanumber',
    'mouseclickdatetime': 'pl_mouseclickdatetime',
    'mouseclickprice': 'pl_mouseclickprice',
    'mouseclickshiftpressed': 'pl_mouseclickshiftpressed',
    'nthmaxlist': 'pl_nthmaxlist',
    'nthminlist': 'pl_nthminlist',
    'openentriescount': 'pl_openentriescount',
    'optiontype': 'pl_optiontype',
    'placemarketorder': 'pl_placemarketorder',
    'playsound': 'pl_playsound',
    'plotpaintbar': 'pl_plotpaintbar',
    'pmm_get_global_named_num': 'pl_pmm_get_global_named_num',
    'pmm_get_global_named_str': 'pl_pmm_get_global_named_str',
    'pmm_get_my_named_num': 'pl_pmm_get_my_named_num',
    'pmm_get_my_named_str': 'pl_pmm_get_my_named_str',
    'pmm_set_global_named_num': 'pl_pmm_set_global_named_num',
    'pmm_set_global_named_str': 'pl_pmm_set_global_named_str',
    'pmm_set_my_named_num': 'pl_pmm_set_my_named_num',
    'pmm_set_my_named_str': 'pl_pmm_set_my_named_str',
    'pmm_set_my_status': 'pl_pmm_set_my_status',
    'pmms_get_strategy_named_num': 'pl_pmms_get_strategy_named_num',
    'pmms_get_strategy_named_str': 'pl_pmms_get_strategy_named_str',
    'pmms_set_strategy_named_num': 'pl_pmms_set_strategy_named_num',
    'pmms_set_strategy_named_str': 'pl_pmms_set_strategy_named_str',
    'pmms_strategies_allow_entries_all': 'pl_pmms_strategies_allow_entries_all',
    'pmms_strategies_count': 'pl_pmms_strategies_count',
    'pmms_strategies_deny_entries_all': 'pl_pmms_strategies_deny_entries_all',
    'pmms_strategies_get_by_symbol_name': 'pl_pmms_strategies_get_by_symbol_name',
    'pmms_strategies_in_long_count': 'pl_pmms_strategies_in_long_count',
    'pmms_strategies_in_short_count': 'pl_pmms_strategies_in_short_count',
    'pmms_strategies_pause_all': 'pl_pmms_strategies_pause_all',
    'pmms_strategies_resume_all': 'pl_pmms_strategies_resume_all',
    'pmms_strategies_set_status_for_all': 'pl_pmms_strategies_set_status_for_all',
    'pmms_strategy_allow_entries': 'pl_pmms_strategy_allow_entries',
    'pmms_strategy_allow_long_entries': 'pl_pmms_strategy_allow_long_entries',
    'pmms_strategy_allow_short_entries': 'pl_pmms_strategy_allow_short_entries',
    'pmms_strategy_currentcontracts': 'pl_pmms_strategy_currentcontracts',
    'pmms_strategy_deny_entries': 'pl_pmms_strategy_deny_entries',
    'pmms_strategy_deny_long_entries': 'pl_pmms_strategy_deny_long_entries',
    'pmms_strategy_deny_short_entries': 'pl_pmms_strategy_deny_short_entries',
    'pmms_strategy_is_paused': 'pl_pmms_strategy_is_paused',
    'pmms_strategy_pause': 'pl_pmms_strategy_pause',
    'pmms_strategy_resume': 'pl_pmms_strategy_resume',
    'pmms_strategy_set_status': 'pl_pmms_strategy_set_status',
    'pmms_strategy_symbol': 'pl_pmms_strategy_symbol',
    'pointvalue': 'pl_pointvalue',
    'portfolio_currencycode': 'pl_portfolio_currencycode',
    'portfolio_currententries': 'pl_portfolio_currententries',
    'portfolio_getmarginpercontract': 'pl_portfolio_getmarginpercontract',
    'portfolio_investedcapital': 'pl_portfolio_investedcapital',
    'portfolio_maxiddrawdown': 'pl_portfolio_maxiddrawdown',
    'portfolio_maxriskequityperpospercent': 'pl_portfolio_maxriskequityperpospercent',
    'portfolio_strategydrawdown': 'pl_portfolio_strategydrawdown',
    'portfolioentriespriority': 'pl_portfolioentriespriority',
    'prevclose': 'pl_prevclose',
    'pricescale': 'pl_pricescale',
    'printer': 'pl_printer',
    'processmouseevents': 'pl_processmouseevents',
    'q_ask': 'pl_q_ask',
    'q_asksize': 'pl_q_asksize',
    'q_bid': 'pl_q_bid',
    'q_bidsize': 'pl_q_bidsize',
    'q_bigpointvalue': 'pl_q_bigpointvalue',
    'q_date': 'pl_q_date',
    'q_exchangelisted': 'pl_q_exchangelisted',
    'q_last': 'pl_q_last',
    'q_openinterest': 'pl_q_openinterest',
    'q_previousclose': 'pl_q_previousclose',
    'q_time': 'pl_q_time',
    'q_time_dt': 'pl_q_time_dt',
    'q_time_s': 'pl_q_time_s',
    'q_totalvolume': 'pl_q_totalvolume',
    'raiseruntimeerror': 'pl_raiseruntimeerror',
    'random': 'pl_random',
    'recalclastbarafter': 'pl_recalclastbarafter',
    'recalcpersist': 'pl_recalcpersist',
    'regularsession': 'pl_regularsession',
    'revsize': 'pl_revsize',
    'rgb': 'pl_rgb',
    'rgbtolegacycolor': 'pl_rgbtolegacycolor',
    'sameexitfromoneentryonce': 'pl_sameexitfromoneentryonce',
    'scrolltobar': 'pl_scrolltobar',
    'secondsfromdatetime': 'pl_secondsfromdatetime',
    'sess1endtime': 'pl_sess1endtime',
    'sess1firstbartime': 'pl_sess1firstbartime',
    'sess1starttime': 'pl_sess1starttime',
    'sess2endtime': 'pl_sess2endtime',
    'sess2firstbartime': 'pl_sess2firstbartime',
    'sess2starttime': 'pl_sess2starttime',
    'sessioncount': 'pl_sessioncount',
    'sessioncountms': 'pl_sessioncountms',
    'sessionendday': 'pl_sessionendday',
    'sessionenddayms': 'pl_sessionenddayms',
    'sessionendtime': 'pl_sessionendtime',
    'sessionendtimems': 'pl_sessionendtimems',
    'sessionlastbar': 'pl_sessionlastbar',
    'sessionstartday': 'pl_sessionstartday',
    'sessionstartdayms': 'pl_sessionstartdayms',
    'sessionstarttime': 'pl_sessionstarttime',
    'sessionstarttimems': 'pl_sessionstarttimems',
    'setbreakeven_pt': 'pl_setbreakeven_pt',
    'setcustomfitnessvalue': 'pl_setcustomfitnessvalue',
    'setfpcompareaccuracy': 'pl_setfpcompareaccuracy',
    'setmaxbarsback': 'pl_setmaxbarsback',
    'setpercenttrailing_pt': 'pl_setpercenttrailing_pt',
    'setprofittarget_pt': 'pl_setprofittarget_pt',
    'setstopcontract': 'pl_setstopcontract',
    'setstoploss_pt': 'pl_setstoploss_pt',
    'setstopshare': 'pl_setstopshare',
    'settrailingstop_pt': 'pl_settrailingstop_pt',
    'sine': 'pl_sine',
    'slippage': 'pl_slippage',
    'squareroot': 'pl_squareroot',
    'strike': 'pl_strike',
    'stringtodate': 'pl_stringtodate',
    'stringtodatetime': 'pl_stringtodatetime',
    'stringtodtformatted': 'pl_stringtodtformatted',
    'stringtotime': 'pl_stringtotime',
    'sumlist': 'pl_sumlist',
    'symbol': 'pl_symbol',
    'symbol_close': 'pl_symbol_close',
    'symbol_date': 'pl_symbol_date',
    'symbol_downticks': 'pl_symbol_downticks',
    'symbol_high': 'pl_symbol_high',
    'symbol_low': 'pl_symbol_low',
    'symbol_open': 'pl_symbol_open',
    'symbol_openint': 'pl_symbol_openint',
    'symbol_tickid': 'pl_symbol_tickid',
    'symbol_ticks': 'pl_symbol_ticks',
    'symbol_time': 'pl_symbol_time',
    'symbol_time_s': 'pl_symbol_time_s',
    'symbol_upticks': 'pl_symbol_upticks',
    'symbol_volume': 'pl_symbol_volume',
    'symbolcurrencycode': 'pl_symbolcurrencycode',
    'symbolname': 'pl_symbolname',
    'tangent': 'pl_tangent',
    'text_anchor_to_bars': 'pl_text_anchor_to_bars',
    'text_get_anchor_to_bars': 'pl_text_get_anchor_to_bars',
    'text_getactive': 'pl_text_getactive',
    'text_getattribute': 'pl_text_getattribute',
    'text_getbarnumber': 'pl_text_getbarnumber',
    'text_getbgcolor': 'pl_text_getbgcolor',
    'text_getborder': 'pl_text_getborder',
    'text_getcolor': 'pl_text_getcolor',
    'text_getdate': 'pl_text_getdate',
    'text_getfirst': 'pl_text_getfirst',
    'text_getfontname': 'pl_text_getfontname',
    'text_gethstyle': 'pl_text_gethstyle',
    'text_getlock': 'pl_text_getlock',
    'text_getnext': 'pl_text_getnext',
    'text_getsize': 'pl_text_getsize',
    'text_gettime': 'pl_text_gettime',
    'text_gettime_dt': 'pl_text_gettime_dt',
    'text_gettime_s': 'pl_text_gettime_s',
    'text_getvalue': 'pl_text_getvalue',
    'text_getvstyle': 'pl_text_getvstyle',
    'text_lock': 'pl_text_lock',
    'text_new_bn': 'pl_text_new_bn',
    'text_new_dt': 'pl_text_new_dt',
    'text_setattribute': 'pl_text_setattribute',
    'text_setbarnumber': 'pl_text_setbarnumber',
    'text_setbgcolor': 'pl_text_setbgcolor',
    'text_setborder': 'pl_text_setborder',
    'text_setlocation_bn': 'pl_text_setlocation_bn',
    'text_setlocation_dt': 'pl_text_setlocation_dt',
    'text_setlocation_s': 'pl_text_setlocation_s',
    'text_setsize': 'pl_text_setsize',
    'tickid': 'pl_tickid',
    'time2time_s': 'pl_time2time_s',
    'time_s2time': 'pl_time_s2time',
    'tl_anchor_to_bars': 'pl_tl_anchor_to_bars',
    'tl_get_anchor_to_bars': 'pl_tl_get_anchor_to_bars',
    'tl_getactive': 'pl_tl_getactive',
    'tl_getalert': 'pl_tl_getalert',
    'tl_getbegin_bn': 'pl_tl_getbegin_bn',
    'tl_getbegin_dt': 'pl_tl_getbegin_dt',
    'tl_getbegindate': 'pl_tl_getbegindate',
    'tl_getbegintime': 'pl_tl_getbegintime',
    'tl_getbegintime_s': 'pl_tl_getbegintime_s',
    'tl_getcolor': 'pl_tl_getcolor',
    'tl_getend_bn': 'pl_tl_getend_bn',
    'tl_getend_dt': 'pl_tl_getend_dt',
    'tl_getenddate': 'pl_tl_getenddate',
    'tl_getendtime': 'pl_tl_getendtime',
    'tl_getendtime_s': 'pl_tl_getendtime_s',
    'tl_getextleft': 'pl_tl_getextleft',
    'tl_getextright': 'pl_tl_getextright',
    'tl_getfirst': 'pl_tl_getfirst',
    'tl_getlock': 'pl_tl_getlock',
    'tl_getnext': 'pl_tl_getnext',
    'tl_getsize': 'pl_tl_getsize',
    'tl_getstyle': 'pl_tl_getstyle',
    'tl_getvalue': 'pl_tl_getvalue',
    'tl_getvalue_bn': 'pl_tl_getvalue_bn',
    'tl_getvalue_dt': 'pl_tl_getvalue_dt',
    'tl_getvalue_s': 'pl_tl_getvalue_s',
    'tl_lock': 'pl_tl_lock',
    'tl_new_bn': 'pl_tl_new_bn',
    'tl_new_dt': 'pl_tl_new_dt',
    'tl_setalert': 'pl_tl_setalert',
    'tl_setbegin_bn': 'pl_tl_setbegin_bn',
    'tl_setbegin_dt': 'pl_tl_setbegin_dt',
    'tl_setbegin_s': 'pl_tl_setbegin_s',
    'tl_setend_bn': 'pl_tl_setend_bn',
    'tl_setend_dt': 'pl_tl_setend_dt',
    'tl_setend_s': 'pl_tl_setend_s',
    'tl_setsize': 'pl_tl_setsize',
    'tool_dashed2': 'pl_tool_dashed2',
    'tool_dashed3': 'pl_tool_dashed3',
    'varsize': 'pl_varsize',
    'varstartaddr': 'pl_varstartaddr',
    'yearfromdatetime': 'pl_yearfromdatetime',
    'yesterday': 'pl_yesterday',
}

# PL comparison operators → Python
COMPARE_OPS = {
    '=': '==',
    '<>': '!=',
    '<': '<',
    '>': '>',
    '>=': '>=',
    '<=': '<=',
}


# Data series that are function parameters (full lists)
_SERIES_PARAMS = {'open', 'high', 'low', 'close', 'volume', 'date', 'time'}


class CodeGen:
    def __init__(self, trace=False, partial=False):
        self.lines = []
        self.indent = 1  # start inside function body
        self.has_once = False
        self.has_math = False
        self._series_context = False  # True when inside function call args
        self.trace = trace
        self._trace_vars = set()  # user-declared variable names to trace
        self._stateful_vars = set()  # vars needing cross-bar persistence (var_decl only)
        self._numeric_vars = set()  # user-declared numeric (not string) vars
        self._double_accum_vars = set()  # self-referential numeric accumulators (kept double, not f32)
        self._array_vars = set()  # user-declared array variable names
        self._need_f32_import = False  # set True when f32/el_round emitted
        self._state_inits = {}  # name -> string initial value for state setdefault
        self._data_refs = {}  # name -> data_ref AST node (Data2-tied vars)
        self._cmt_lead = {}   # id(stmt) -> [verbatim EL comment, ...] (Rcmt clean mode)
        self._cmt_trail = {}  # id(stmt) -> [verbatim EL comment, ...] (Rcmt clean mode)
        self._cur_stmt_line = None  # nearest enclosing statement's EL line (for error messages)
        self._unimpl_errors = []  # collect-all: {'name','line'} for every unimplemented keyword
        self._partial = partial  # FL2: opt-in partial mode (unimplemented -> exec-time stubs)
        self._declared_all = set()  # EVERY var/input/array/for name at ANY depth (F3 flagging)

    def emit(self, line):
        self.lines.append('    ' * self.indent + line)

    def gen_program(self, node):
        # Reset the collect-all diagnostics for this generate() (no cross-call leak).
        self._unimpl_errors = []
        # Collect EVERY declared name (Variables:/Inputs:/Arrays:/for-loop vars) at
        # EVERY nesting depth — Variables: blocks are legally declared INSIDE Begin/End
        # bodies (if/for/while/switch cases) mid-program, and round-tripped/emitted EL
        # re-positions them. The F3 bare-keyword flag uses this set as the "is a real
        # declared identifier" oracle; a shallow (top-level-only) pass would wrongly
        # flag nested-declared vars as unimplemented. _collect_declared already walks
        # every nested body recursively, so it is the single source of truth here.
        from pl_transpiler.parser import _collect_declared
        self._declared_all = _collect_declared(node)
        # A bare ident that is ASSIGNED anywhere (at any depth) can be a legitimate
        # local — a plain `x = ...` statement DEFINES x for the emitted Python, exactly
        # as the forward path's `return name` always relied on. This matters for
        # round-tripped EL: py_front cannot tell an original `Variables:` declaration
        # from a plain assignment, so a nested-declared var comes back as a bare
        # `x = ...`; forward-transpiling that must NOT flag x's reads.
        #   FL5 RC3: but this may ONLY whitelist targets whose value is also USED (read)
        # somewhere. A name that is ONLY EVER an assignment TARGET and never read, and
        # is not otherwise known (not Value*/Condition*/declared), is an assignment to a
        # read-only/unimplemented EL keyword (e.g. `AvgWinTrade = Close`, which EL
        # forbids) — treating it as a local silently BYPASSES the strict fail-loud.
        # Gating on "also read" keeps genuine `x = ...; y = x` locals valid while making
        # a write-only unimplemented keyword collect+raise (strict) / stub (partial).
        #   FL6 BUG C: "assigned & read" was still too broad. A recognized-but-
        # UNIMPLEMENTED EL keyword (e.g. FastD — a builtin marker with no faithful
        # bare expansion) or an unknown keyword that is read BEFORE it is ever
        # cleanly defined (`Value1 = FakeA; FakeA = 2;`) or is only ever self-
        # referential (`AvgWinTrade = AvgWinTrade + 1;`) is NOT a genuine local — it
        # has no defining write establishing a value, so rescuing it silently
        # bypasses the strict fail-loud (and drops it from the collect-all report).
        # _rescuable_assign_locals keeps ONLY names that (a) are not otherwise
        # recognized as a builtin/reserved keyword AND (b) have a clean defining
        # assignment (target not read on its own RHS) BEFORE their first read in
        # evaluation order — exactly the flattened nested-`Variables:` locals the
        # reverse path emits as bare `x = 0; ... = x`.
        _assigned = set()
        self._collect_assigned_targets(node, _assigned)
        _read = set()
        self._collect_read_names(node, _read)
        self._declared_all |= self._rescuable_assign_locals(node, _assigned & _read)
        # Collect user-declared var names first (before codegen body)
        for stmt in node['body']:
            if stmt['type'] == 'var_decl':
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    if name:
                        self._trace_vars.add(name)
                        self._stateful_vars.add(name)
                        # Check for Data2-tied initializer: Varname( expr, DataN )
                        if d.get('data_ref') is not None:
                            self._data_refs[name] = d['data_ref']
                            self._numeric_vars.add(name)
                            continue
                        # Determine if numeric: init is a number AST node
                        init = d.get('init', {})
                        if init.get('type') == 'number':
                            self._numeric_vars.add(name)
                            self._state_inits[name] = init['value']
                        elif init.get('type') == 'boolean':
                            self._state_inits[name] = '1' if init.get('value') else '0'
                        elif init.get('type') == 'string':
                            self._state_inits[name] = repr(init.get('value', ''))
                        else:
                            self._state_inits[name] = '0' if name in self._numeric_vars else "''"
            elif stmt['type'] == 'input_decl':
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    if name:
                        self._trace_vars.add(name)
                        default = d.get('default', {})
                        if default.get('type') == 'number':
                            self._numeric_vars.add(name)
            elif stmt['type'] == 'array_decl':
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    if name:
                        self._trace_vars.add(name)
                        self._array_vars.add(name)

        # Detect self-referential numeric accumulators: assignments of the form
        # `Var = ...Var...` (Var referenced on its own RHS, as ident or Var[n]).
        # EL/PowerLanguage numeric variables are DOUBLE precision; an unbounded
        # accumulator that is f32-rounded every bar drifts at full range (proven by
        # GT1's runsum capture, which matches a plain float64 cumulative sum exactly).
        # We keep ONLY these self-referential numeric vars in double; every other
        # numeric var retains the existing f32 rounding (a blanket flip to double
        # regresses long-range trade parity, so we narrow per the capture).
        self._collect_double_accums(node['body'])

        # FL2/F1: in partial (trace) mode, a `Vars: x(<init containing an
        # unimplemented keyword>)` must make the RUN fail loud the moment x is
        # initialized — not silently start x at a literal default. Trace-mode
        # gen_var_decl reads each stateful var from _state and DISCARDS the
        # generated initializer, so the stub call that _require_pl_impl produces
        # for the unknown keyword would never reach the emitted body. Detect an
        # unimplemented keyword in each top-level var initializer here (walk it to
        # collect the same {name,line} the watermark reports) and, when found, make
        # the var's state SEED be the argless stub call, so the emitted
        # `_state.setdefault('x', [pl_partial_stub('x', <line>)()])` raises
        # UnimplementedKeywordError on the first bar. Strict (non-partial) mode is
        # untouched — it raises at transpile time via _raise_unimplemented().
        if self._partial and self.trace:
            for stmt in node['body']:
                if stmt.get('type') != 'var_decl':
                    continue
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    init = d.get('init')
                    if (not name or not isinstance(init, dict)
                            or d.get('data_ref') is not None):
                        continue
                    before = len(self._unimpl_errors)
                    self.gen_expr(init)  # walk only — collect unimplemented keywords
                    if len(self._unimpl_errors) > before:
                        # Seed the var's state with an ARGLESS stub naming the first
                        # unimplemented keyword found in the initializer (accurate
                        # message, consistent with the watermark). A bare stub (not
                        # the full init expression) guarantees the run raises
                        # UnimplementedKeywordError — never some unrelated error from
                        # evaluating the rest of the initializer.
                        e = self._unimpl_errors[before]
                        self._state_inits[name] = (
                            f"pl_partial_stub({e['name']!r}, {e['line']!r})()")

        # Emit state initialization (trace mode, stateful vars only)
        if self.trace and self._stateful_vars:
            self.emit("_state = kwargs.setdefault('_state', {})")
            for name in sorted(self._stateful_vars):
                init_val = self._state_inits.get(name, '0')
                # Check if this var has a Data2-tied initializer
                data_ref = self._data_refs.get(name)
                if data_ref:
                    # Data2-tied: initial value comes from Data2 series via kwargs
                    self.emit(f"_state.setdefault('{name}', [kwargs.get('data2_close', close[-1])])")
                else:
                    self.emit(f"_state.setdefault('{name}', [{init_val}])")
        # Rcmt: in clean (trace=False) mode, carry the source EL comments through as
        # `#| <verbatim>` lines, anchored (by object identity) to the same statements
        # the reverse emitter uses. Trace mode omits them (the dumped values must stay
        # byte-identical). Maps are read by gen_stmt; header/footer emitted here.
        self._cmt_lead, self._cmt_trail = {}, {}
        if not self.trace:
            from pl_transpiler.codegen_el import _anchor_comments
            _hdr, _lead, _trail, _ftr = _anchor_comments(
                node['body'], node.get('_comments') or [])
            self._cmt_lead, self._cmt_trail = _lead, _trail
            for txt in _hdr:
                self._emit_comment(txt)

        # Emit the wrapper function body
        for stmt in node['body']:
            self.gen_stmt(stmt)

        if not self.trace:
            for txt in _ftr:
                self._emit_comment(txt)

        # Emit state append + trace at end of bar (before return)
        if self.trace:
            for name in sorted(self._stateful_vars):
                self.emit(f"_state['{name}'].append({name})")
            # Save arrays directly (not appending — they are mutable lists mutated in place)
            for name in sorted(self._array_vars):
                self.emit(f"_state['{name}'] = {name}")
            for name in sorted(self._trace_vars):
                self.emit(f"_trace['{name}'] = {name}")

        # Collect-all fail-loud: if any unimplemented keyword was recorded during the
        # walk, discard the emitted text and raise ONE complete report (no Python is
        # ever returned when an unknown keyword exists — strict all-or-nothing).
        # In partial mode (opt-in) we do NOT raise: each unknown was emitted as an
        # execution-time stub and the output is watermarked below instead.
        if self._unimpl_errors and not self._partial:
            self._raise_unimplemented()

        # Build output
        header = (
            "def strategy(open, high, low, close, volume, date, time, **kwargs):\n"
            "    # Ensure series params are lists\n"
            "    if not isinstance(close, list): open, high, low, close, volume, date, time = [open],[high],[low],[close],[volume],[date],[time]\n"
            "    _orders = []\n"
            "    _plots = {}\n"
            "    _alerts = []\n"
            "    _risk = {}\n"
            "    _commentary = None\n"
            "    _drawings = []\n"
            "    _market_position = kwargs.get('market_position', 0)\n"
            "    _current_contracts = kwargs.get('current_contracts', 0)\n"
            "    time_s = kwargs.get('time_s', 0)\n"
        )
        if self.has_once:
            header += "    _first_bar = kwargs.get('_first_bar', True)\n"
        if self.trace:
            header += "    _trace = {}\n"
            header += "    _trace['_barnumber'] = kwargs.get('barnumber', 0)\n"
            header += "    _trace['_date'] = date[-1] if isinstance(date, list) else date\n"
            header += "    _trace['_time'] = time[-1] if isinstance(time, list) else time\n"
            header += "    _trace['_close'] = close[-1] if isinstance(close, list) else close\n"

        body = '\n'.join(self.lines)
        return_items = ["'orders': _orders", "'plots': _plots", "'alerts': _alerts", "'risk': _risk"]
        if self.trace:
            return_items.append("'_trace': _trace")
        footer = "\n    return {" + ", ".join(return_items) + "}\n"

        imports = ''
        if self.has_math:
            imports = 'import math\n'
        # Import runtime module (includes f32, el_round, pl_* functions)
        imports += 'from pl_transpiler.runtime.pl_runtime import f32, el_round0, el_round, pl_file\n'
        imports += 'from pl_transpiler.runtime.pl_runtime import *\n'
        # FL2: partial output that actually emitted stubs imports the stub factory
        # explicitly. This line is added ONLY for partial output with N>0 stubs, so
        # the strict transpile stays byte-identical (proven by the corpus baseline).
        if self._partial and self._unimpl_errors:
            imports += 'from pl_transpiler.runtime.pl_runtime import pl_partial_stub\n'
        imports += '__builtin_len = len\n'

        out = imports + header + body + footer

        if self._partial:
            errors = self._sorted_unimpl()
            if errors:
                return self._partial_watermark(errors) + out
            # No unimplemented constructs: output is byte-identical to the strict
            # transpile, preceded by exactly one acknowledgement line.
            return (
                "# PARTIAL TRANSPILE requested — 0 unimplemented constructs; "
                "output identical to the strict transpile.\n" + out
            )
        return out

    def _expr_refs(self, node, names):
        """Collect into `names` every user-variable referenced in expr `node`
        (bare ident `Var` and bar-ref `Var[n]`)."""
        if not isinstance(node, dict):
            return
        t = node.get('type')
        if t == 'ident':
            names.add(node.get('name'))
        elif t == 'bar_ref':
            series = node.get('series') or {}
            if series.get('type') == 'ident':
                names.add(series.get('name'))
            self._expr_refs(node.get('index'), names)
        # Recurse into child expression nodes generically.
        for key in ('left', 'right', 'operand', 'cond', 'msg', 'value', 'init',
                    'start', 'end', 'index'):
            if key in node:
                self._expr_refs(node[key], names)
        for key in ('args', 'decls'):
            for child in node.get(key, []) or []:
                self._expr_refs(child, names)

    def _collect_double_accums(self, stmts):
        """Walk all statements; mark numeric vars assigned from an expression that
        references themselves (self-referential cross-bar accumulators)."""
        for stmt in stmts or []:
            if not isinstance(stmt, dict):
                continue
            if stmt.get('type') == 'assign':
                target = stmt.get('target', {})
                if target.get('type') == 'ident':
                    tname = target.get('name')
                    if tname in self._numeric_vars:
                        refs = set()
                        self._expr_refs(stmt.get('value'), refs)
                        if tname in refs:
                            self._double_accum_vars.add(tname)
            # Recurse into nested bodies (if/else/for/while).
            for key in ('body', 'else_body'):
                self._collect_double_accums(stmt.get(key))

    def _collect_assigned_targets(self, node, out):
        """Recursively add into `out` the name of every ident that appears as an
        assignment TARGET anywhere in the AST (at any nesting depth). Used by the F3
        bare-keyword flag so a name defined purely by assignment (never declared) is
        treated as a known local rather than an unimplemented keyword."""
        if isinstance(node, dict):
            if node.get('type') == 'assign':
                tgt = node.get('target')
                if isinstance(tgt, dict) and tgt.get('type') == 'ident':
                    out.add(tgt['name'])
                # Stochastic-style multi-output: the trailing call args are output
                # var names the assignment defines.
                val = node.get('value')
                if (isinstance(val, dict) and val.get('type') == 'call'
                        and val.get('name') == 'stochastic'):
                    for a in val.get('args', [])[7:]:
                        if isinstance(a, dict) and a.get('type') == 'ident':
                            out.add(a['name'])
            for v in node.values():
                self._collect_assigned_targets(v, out)
        elif isinstance(node, list):
            for x in node:
                self._collect_assigned_targets(x, out)

    def _collect_read_names(self, node, out):
        """Recursively add into `out` every identifier that appears in a READ (value)
        position anywhere in the AST — a bare ident, or the series name of a bar_ref.
        An assignment TARGET is a WRITE, not a read, so its target ident is skipped
        (but the assignment's RHS, and a subscript index on a bar_ref target `Arr[i]`,
        ARE reads and are walked). Used by the FL5 RC3 lvalue guard so that only an
        assignment target that is ALSO read somewhere is whitelisted as a legitimate
        local — a write-only unimplemented EL keyword (`AvgWinTrade = Close`) is not."""
        if isinstance(node, dict):
            t = node.get('type')
            if t == 'ident':
                if node.get('name'):
                    out.add(node['name'])
                return
            if t == 'bar_ref':
                series = node.get('series') or {}
                if series.get('type') == 'ident':
                    if series.get('name'):
                        out.add(series['name'])
                else:
                    self._collect_read_names(series, out)
                self._collect_read_names(node.get('index'), out)
                return
            if t == 'assign':
                self._collect_read_names(node.get('value'), out)
                tgt = node.get('target')
                if isinstance(tgt, dict) and tgt.get('type') == 'bar_ref':
                    self._collect_read_names(tgt.get('index'), out)
                return
            for v in node.values():
                self._collect_read_names(v, out)
        elif isinstance(node, list):
            for x in node:
                self._collect_read_names(x, out)

    def _ordered_ident_events(self, node, events):
        """Append (kind, name) identifier events in EVALUATION order. kind is
        'read' for an identifier used as a value; 'cleandef' for an assignment
        whose target ident is NOT read on its own RHS (a defining write that
        establishes a value independent of the target's prior state). An `assign`
        evaluates its RHS (reads) before the target is written, and a control-flow
        node evaluates its condition/bounds (reads, in source order) before its
        body — the generic in-order recursion preserves both. Feeds the FL6 BUG C
        rescue analysis (a genuine local is cleanly defined before its first read;
        a read-before-write or self-referential unimplemented keyword is not)."""
        if isinstance(node, list):
            for x in node:
                self._ordered_ident_events(x, events)
            return
        if not isinstance(node, dict):
            return
        t = node.get('type')
        if t == 'ident':
            if node.get('name'):
                events.append(('read', node['name']))
            return
        if t == 'bar_ref':
            series = node.get('series') or {}
            if series.get('type') == 'ident':
                if series.get('name'):
                    events.append(('read', series['name']))
            else:
                self._ordered_ident_events(series, events)
            self._ordered_ident_events(node.get('index'), events)
            return
        if t == 'assign':
            # RHS is evaluated (reads) before the target is assigned.
            self._ordered_ident_events(node.get('value'), events)
            rhs_reads = set()
            self._collect_read_names(node.get('value'), rhs_reads)
            tgt = node.get('target')
            if isinstance(tgt, dict) and tgt.get('type') == 'ident' and tgt.get('name'):
                name = tgt['name']
                # A "clean" define establishes a value not derived from the
                # target's own prior state (target not read on the RHS). A self-
                # referential write (`X = X + 1`) emits no cleandef — the RHS read
                # event above already records X (and marks it broken if undefined).
                if name not in rhs_reads:
                    events.append(('cleandef', name))
            elif isinstance(tgt, dict) and tgt.get('type') == 'bar_ref':
                # Array/element write: the subscript index is a read; the series
                # name is an array, handled via declarations, not a scalar define.
                self._ordered_ident_events(tgt.get('index'), events)
            return
        for v in node.values():
            self._ordered_ident_events(v, events)

    def _rescuable_assign_locals(self, node, assigned_and_read):
        """From the set of names that are BOTH assigned and read (`_assigned &
        _read`), return only those safe to treat as genuine locals-by-assignment.

        Excluded (so the strict fail-loud path still flags them):
          * any name otherwise recognized as a builtin/reserved keyword or a
            resolvable pl_ target (e.g. FastD) — a write to it is not a local decl;
          * any name whose first appearance in evaluation order is a READ, or whose
            only defines are self-referential (`X = X + 1` with no prior clean
            define) — it has no value-establishing write (FakeA read-before-write,
            AvgWinTrade self-accumulate), so it is a read of an undefined name.
        Kept: names with a clean defining write BEFORE their first read — exactly
        the flattened nested-`Variables:` locals the reverse path emits as bare
        `x = 0; ...; y = x` (BUG C)."""
        if not assigned_and_read:
            return set()
        from pl_transpiler.parser import _is_known_name, BUILTIN_FUNC_MAP
        from pl_transpiler import catalog
        candidates = set()
        for name in assigned_and_read:
            # FAIL-CLOSED (a2 audit F2): a name that is a RECOGNIZED EL keyword
            # (catalog signature present — e.g. a strategy-performance / report
            # keyword such as AvgBarsWinTrade) is read-only in EL and can NEVER be a
            # genuine round-tripped `Variables:` local (EL forbids declaring a var
            # named after a reserved keyword). Rescuing it as a local on the strength
            # of a `Kw = <expr>` write (which EL itself rejects) silently emits a
            # wrong value and drops it from the collect-all report. Exclude any
            # catalog-known keyword so a write+read of one still fails loud.
            if (name in self._declared_all
                    or name in BUILTIN_FUNC_MAP
                    or f'pl_{name}' in _available_pl_targets()
                    or catalog.get(name) is not None
                    or _is_known_name(name, self._declared_all)):
                continue  # otherwise-known keyword — never rescue as a local
            candidates.add(name)
        if not candidates:
            return set()
        events = []
        self._ordered_ident_events(node, events)
        clean_defined = set()
        broken = set()
        for kind, name in events:
            if name not in candidates:
                continue
            if kind == 'read':
                if name not in clean_defined:
                    broken.add(name)
            else:  # 'cleandef'
                clean_defined.add(name)
        return (candidates & clean_defined) - broken

    def _emit_comment(self, text):
        """Emit a verbatim EL comment as `#| ` line(s) (Rcmt, clean mode only). A
        multi-line `{...}` comment keeps its internal newlines, one `#|` line each."""
        for phys in text.split('\n'):
            self.emit('#| ' + phys)

    def gen_stmt(self, node):
        for txt in self._cmt_lead.get(id(node), ()):
            self._emit_comment(txt)
        start = len(self.lines)          # first physical line this statement emits
        if node.get('_line'):
            self._cur_stmt_line = node['_line']
        ntype = node['type']
        method = getattr(self, f'gen_{ntype}', None)
        if method:
            method(node)
        else:
            raise ValueError(f"Unknown statement type: {ntype}")
        # Trailing comment (anchored to this statement's own source line): emit it as a
        # same-line `#|` suffix on the statement's FIRST emitted line (which corresponds
        # to the EL statement's start line), so py_front recovers its trailing-ness and
        # re-anchors it to THIS statement — not the next one. Single-`#|`-line texts only
        # (a trailing anchor is always a one-line `{...}`); a multi-physical-line block
        # comment is never trailing. Falls back to an own-line comment if the statement
        # somehow emitted nothing.
        trail = self._cmt_trail.get(id(node))
        if trail:
            if len(self.lines) > start and all('\n' not in t for t in trail):
                self.lines[start] += '  ' + ' '.join('#| ' + t for t in trail)
            else:
                for txt in trail:
                    self._emit_comment(txt)

    def gen_attribute(self, node):
        # Strategy/study attribute (e.g. [SameExitFromOneEntryOnce = True]) — a
        # declaration with no effect on the per-bar dumped column values, so it
        # emits nothing. Recorded as a comment for traceability.
        self.emit(f"# attribute [{node.get('name')}] (no-op for calc/trace)")

    # ---- Variable Declarations ----
    def gen_var_decl(self, node):
        for d in node['decls']:
            name = d['name']
            init_val = self.gen_expr(d['init'])
            if name in self._array_vars:
                # Arrays handled by gen_array_decl
                self.emit(f"{name} = _state.get('{name}', [{init_val}] * 1)")
            elif self.trace and name in self._stateful_vars:
                # Read current value from state history list (previous bar's end value).
                # Self-referential numeric accumulators persist in DOUBLE precision
                # (re-wrapping in f32() every bar would drift an unbounded accumulator
                # like RunSum at full range — see _collect_double_accums); all other
                # numeric vars keep the existing per-bar f32 rounding.
                if name in self._numeric_vars and name not in self._double_accum_vars:
                    self.emit(f"{name} = f32(_state['{name}'][-1])")
                    self._need_f32_import = True
                else:
                    self.emit(f"{name} = _state['{name}'][-1]")
            else:
                # Non-trace mode: use init value as before
                if name in self._numeric_vars:
                    self.emit(f"{name} = f32({init_val})")
                    self._need_f32_import = True
                else:
                    self.emit(f"{name} = {init_val}")

    # ---- Input Declarations ----
    def gen_input_decl(self, node):
        for d in node['decls']:
            name = d['name']
            default_val = self.gen_expr(d['default'])
            val = f"kwargs.get({name!r}, {default_val})"
            self.emit(f"{name} = {val}")
            if self.trace and name in self._trace_vars:
                self.emit(f"_trace['{name}'] = {name}")

    # ---- If Statement ----
    def gen_if(self, node):
        cond = self.gen_expr(node['cond'])
        self.emit(f"if {cond}:")
        self.indent += 1
        if node['body']:
            for s in node['body']:
                self.gen_stmt(s)
        else:
            self.emit('pass')
        self.indent -= 1

        if node.get('else_body'):
            self.emit('else:')
            self.indent += 1
            for s in node['else_body']:
                self.gen_stmt(s)
            self.indent -= 1

    # ---- For Loop ----
    def gen_for(self, node):
        var = node['var']
        start = self.gen_expr(node['start'])
        end = self.gen_expr(node['end'])
        # Wrap in int() because EL for-loop bounds may be float32 (e.g. f32(len))
        if node['downto']:
            self.emit(f"for {var} in range(int({start}), int({end}) - 1, -1):")
        else:
            self.emit(f"for {var} in range(int({start}), int({end}) + 1):")
        self.indent += 1
        if node['body']:
            for s in node['body']:
                self.gen_stmt(s)
        else:
            self.emit('pass')
        self.indent -= 1

    # ---- While Loop ----
    def gen_while(self, node):
        cond = self.gen_expr(node['cond'])
        self.emit(f"while {cond}:")
        self.indent += 1
        if node['body']:
            for s in node['body']:
                self.gen_stmt(s)
        else:
            self.emit('pass')
        self.indent -= 1

    # ---- Repeat..Until ----
    def gen_repeat_until(self, node):
        cond = self.gen_expr(node['cond'])
        self.emit("while True:")
        self.indent += 1
        if node['body']:
            for s in node['body']:
                self.gen_stmt(s)
        self.emit(f"if {cond}:")
        self.indent += 1
        self.emit("break")
        self.indent -= 1
        self.indent -= 1

    # ---- Once ----
    def gen_once(self, node):
        self.has_once = True
        self.emit("if _first_bar:")
        self.indent += 1
        for s in node['body']:
            self.gen_stmt(s)
        self.emit("_first_bar = False")
        self.indent -= 1

    # ---- Switch/Case ----
    def gen_switch(self, node):
        expr = self.gen_expr(node['expr'])
        # FAIL-CLOSED (a1 audit F1/F2): the switch subject was previously emitted
        # ONLY inside the per-case `if/elif <expr> == <val>:` lines. With ZERO case
        # labels (`Switch (X) Begin End;` or a Default-only switch) that loop emits
        # nothing, so the subject expression — and, in partial mode, any
        # pl_partial_stub(...)() it carries — was silently DROPPED (the run then
        # completes with a wrong value while the watermark claims a stub) and a
        # Default-only switch emitted an orphan `else:` (a SyntaxError). EL always
        # evaluates the subject once regardless of case matches, so with no case
        # labels emit the subject as a standalone statement (evaluating it — and
        # firing any stub) and, if present, run the Default body unconditionally
        # (nothing can match). The normal (>=1 case) path below is UNCHANGED, so
        # existing corpus output stays byte-identical.
        if not node['cases']:
            self.emit(expr)
            if node.get('default'):
                for s in node['default']:
                    self.gen_stmt(s)
            return
        for i, case in enumerate(node['cases']):
            val = self.gen_expr(case['value'])
            keyword = 'if' if i == 0 else 'elif'
            self.emit(f"{keyword} {expr} == {val}:")
            self.indent += 1
            if case['body']:
                for s in case['body']:
                    self.gen_stmt(s)
            else:
                self.emit('pass')
            self.indent -= 1
        if node.get('default'):
            self.emit('else:')
            self.indent += 1
            for s in node['default']:
                self.gen_stmt(s)
            self.indent -= 1

    # ---- Array Declaration ----
    def gen_array_decl(self, node):
        for d in node['decls']:
            name = d['name']
            init = self.gen_expr(d['init'])
            if d['size'] is None:
                # Dynamic array: starts as a single-element list, grown by pl_array_setmaxindex
                default = f"[{init}]"
            else:
                size = self.gen_expr(d['size'])
                default = f"[{init}] * ({size} + 1)"
            if self.trace:
                # Load array from state (persistent between bars); when absent,
                # initialize the SAME sized list the non-trace path builds.
                self.emit(f"{name} = _state.get('{name}', {default})")
            else:
                self.emit(f"{name} = {default}")
            if self.trace and name in self._trace_vars:
                self.emit(f"_trace['{name}'] = {name}")

    # ---- Commentary ----
    def gen_commentary(self, node):
        self.emit(f"_commentary = {self.gen_expr(node['msg'])}")

    # ---- Break / Continue ----
    def gen_break(self, node):
        self.emit('break')

    def gen_continue(self, node):
        self.emit('continue')

    # ---- Abort ----
    def gen_abort(self, node):
        # EL Abort halts the run-time calculation. In transpiled traces it is
        # only reached in never-true compile-coverage branches, so emit a no-op.
        self.emit('pass  # Abort (no-op for calc/trace)')

    # ---- Assignment ----
    def gen_assign(self, node):
        target = self.gen_expr(node['target'])
        # FL5 RC1/F2: if the assignment TARGET is itself an unimplemented keyword,
        # gen_expr returned a stub call `pl_partial_stub('<name>', <line>)()` (partial
        # mode only — strict records+raises at generate end and never reaches here).
        # Emitting `pl_partial_stub(...)() = rhs` is a Python SyntaxError. Emit the
        # stub as a STANDALONE statement instead, so execution raises
        # UnimplementedKeywordError at this line and the file still compiles. The RHS
        # is still WALKED (for watermark completeness) but its emitted text is dropped.
        if (self._partial and isinstance(target, str)
                and target.startswith('pl_partial_stub(')):
            self.gen_expr(node['value'])
            self.emit(target)
            return
        # Handle multi-output Stochastic: returns (status, FastK, FastD, SlowK, SlowD)
        if node['value']['type'] == 'call' and node['value']['name'] == 'stochastic':
            self._gen_stochastic_assign(target, node['value'])
            return
        value = self.gen_expr(node['value'])
        # Wrap with f32 for numeric user variables, EXCEPT self-referential
        # accumulators (RunSum = RunSum + Close), which EL keeps in double precision.
        # The pl_* runtime fns self-round their results and data series are f32 at
        # access, so dropping the wrap here keeps an unbounded accumulator in double
        # (matching the GT1 runsum capture) without affecting function/data values.
        if (node['target']['type'] == 'ident'
                and node['target']['name'] in self._numeric_vars
                and node['target']['name'] not in self._double_accum_vars):
            value = f"f32({value})"
            self._need_f32_import = True
        self.emit(f"{target} = {value}")
        # Trace user-variable assignments
        if self.trace and node['target']['type'] == 'ident':
            vname = node['target']['name']
            if vname in self._trace_vars:
                self.emit(f"_trace['{vname}'] = {vname}")

    def _gen_stochastic_assign(self, target, call_node):
        """Generate multi-statement unpacking for Stochastic's (status, FastK, FastD, SlowK, SlowD) tuple."""
        # Args: high, low, close, length, smoothk, smoothd, smoothtype, oFastK, oFastD, oSlowK, oSlowD
        input_args = call_node['args'][:7]  # first 7 are inputs
        output_names = [a['name'] for a in call_node['args'][7:]]  # last 4 are output var names
        
        # Process input args with series context
        old_ctx = self._series_context
        self._series_context = True
        arg_exprs = [self.gen_expr(a) for a in input_args]
        self._series_context = old_ctx
        
        call_str = f"pl_stochastic({', '.join(arg_exprs)})"
        result_var = "_stoch_result"
        
        self.emit(f"{result_var} = {call_str}")
        # Unpack: [0]=status->target, [1]=FastK, [2]=FastD, [3]=SlowK, [4]=SlowD
        # Target (stochret) gets status at [0]
        if target in self._numeric_vars:
            self.emit(f"{target} = f32({result_var}[0])")
            self._need_f32_import = True
        else:
            self.emit(f"{target} = {result_var}[0]")
        if self.trace and target in self._trace_vars:
            self.emit(f"_trace['{target}'] = {target}")
        # Output vars get elements [1]..[4]
        for i, name in enumerate(output_names):
            if name in self._numeric_vars:
                self.emit(f"{name} = f32({result_var}[{i + 1}])")
                self._need_f32_import = True
            else:
                self.emit(f"{name} = {result_var}[{i + 1}]")
            if self.trace and name in self._trace_vars:
                self.emit(f"_trace['{name}'] = {name}")

    # ---- Expression Statement ----
    def gen_expr_stmt(self, node):
        self.emit(self.gen_expr(node['expr']))

    # ---- Order ----
    def gen_order(self, node):
        action = node['action']
        otype = node['order_type']
        label = node.get('label', '')
        # label can be a string (old-style literal) or an AST dict (expression)
        if isinstance(label, dict):
            label_expr = self.gen_expr(label)
        else:
            label_expr = repr(label)
        qty = node.get('quantity')
        if qty and isinstance(qty, dict) and qty.get('name') == 'all':
            # EL 'all' = entire position: emit current_contracts from runtime
            qty_expr = '_current_contracts'
        elif qty:
            qty_expr = self.gen_expr(qty)
        else:
            qty_expr = '0'
        # Optional "from entry("label")" reference (mode 4 named exits)
        entry_label = node.get('entry_label')
        if entry_label is not None:
            entry_label_expr = self.gen_expr(entry_label) if isinstance(entry_label, dict) else repr(entry_label)
        else:
            entry_label_expr = None

        timing = node.get('bar_timing', 'next')
        # The `total` order modifier (EL: `... N contracts total ...`) is a rare,
        # position-sizing-scope flag. It is appended as a trailing sentinel string
        # 'total' ONLY when present, so non-total orders keep byte-identical tuples
        # (every GT source) and the FillEngine's type-based price detection at
        # order[5] is unaffected. py_front._inv_order strips it back to node['total'].
        total_suffix = ", 'total'" if node.get('total') else ""
        if node.get('price'):
            price_str = self.gen_expr(node['price'])
            if entry_label_expr:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}, {price_str}, {entry_label_expr}{total_suffix}))")
            else:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}, {price_str}{total_suffix}))")
        else:
            if entry_label_expr:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}, {entry_label_expr}{total_suffix}))")
            else:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}{total_suffix}))")

    # ---- Risk ----
    def gen_risk(self, node):
        func = node['func']
        if func == 'setstoploss':
            self.emit(f"_risk['stop_loss'] = {self.gen_expr(node['arg'])}")
        elif func == 'setprofittarget':
            self.emit(f"_risk['profit_target'] = {self.gen_expr(node['arg'])}")
        elif func == 'setexitonclose':
            self.emit("_risk['exit_on_close'] = True")
        elif func == 'setdollartrailing':
            self.emit(f"_risk['dollar_trailing'] = {self.gen_expr(node['arg'])}")
        elif func == 'setpercenttrailing':
            args = node.get('args', [node.get('arg')])
            profit = self.gen_expr(args[0])
            pct = self.gen_expr(args[1]) if len(args) > 1 else '0'
            self.emit(f"_risk['percent_trailing'] = ({profit}, {pct})")
        elif func == 'setstopposition':
            if node.get('arg') is not None:
                self.emit(f"_risk['stop_position'] = {self.gen_expr(node['arg'])}")
            else:
                self.emit("_risk['stop_position'] = True")
        elif func == 'setstopcontract':
            self.emit("_risk['stop_contract'] = True")
        elif func == 'setstopshare':
            self.emit("_risk['stop_share'] = True")
        elif func == 'setbreakeven':
            self.emit(f"_risk['breakeven'] = {self.gen_expr(node['arg'])}")

    # ---- Plot ----
    def gen_plot(self, node):
        name = node['name']
        value = self.gen_expr(node['value'])
        self.emit(f"_plots.setdefault('{name}', []).append({value})")
        if node.get('label'):
            label = node['label']
            if isinstance(label, dict):
                # Label is an AST expression node (variable, etc.)
                self.emit(f"_plots['{name}_label'] = {self.gen_expr(label)}")
            else:
                # Label is a plain string
                self.emit(f"_plots['{name}_label'] = '{label}'")

    # ---- Alert ----
    def gen_alert(self, node):
        self.emit(f"_alerts.append({self.gen_expr(node['msg'])})")

    # ---- Print ----
    def gen_print(self, node):
        args = ', '.join(self.gen_expr(a) for a in node['args'])
        self.emit(f"print({args})")

    # ==== Expression Generation ====
    def gen_expr(self, node):
        ntype = node['type']

        if ntype == 'number':
            val = node['value']
            # Strip leading zeros from integer literals to avoid Python octal errors
            # but preserve "0" itself and floats like "0.5"
            if '.' not in val:
                val = val.lstrip('0') or '0'
            return val

        if ntype == 'string':
            return repr(node['value'])

        if ntype == 'boolean':
            return 'True' if node['value'] else 'False'

        if ntype == 'ident':
            name = node['name']
            # Data series params: emit current-bar value unless in series context
            if name in _SERIES_PARAMS and not self._series_context:
                # Wrap OHLCV data in f32 for EL float32 fidelity (ad, #U10)
                if name in ('open', 'high', 'low', 'close', 'volume'):
                    return f"f32({name}[-1])"
                return f"{name}[-1]"
            # In series context, user variables should pass their history list
            # Include the current bar's computed value (state hasn't been appended yet)
            if self._series_context and name in self._stateful_vars:
                return f"(_state['{name}'] + [{name}])"
            # Bare reserved words that compute values from bar data
            _BARE_COMPUTED = {
                'truerange': "pl_true_range(high[-1], low[-1], (close[-2] if close[-2] != 0.0 else open[-1]))",
                'medianprice': "((high[-1] + low[-1]) / 2.0)",
                'range': "(high[-1] - low[-1])",
                'avgprice': "((open[-1] + high[-1] + low[-1] + close[-1]) / 4.0)",
                'truehigh': "max(high[-1], (close[-2] if close[-2] != 0.0 else open[-1]))",
                'truelow': "min(low[-1], (close[-2] if close[-2] != 0.0 else open[-1]))",
                'typicalprice': "((high[-1] + low[-1] + close[-1]) / 3.0)",
                'weightedclose': "((high[-1] + low[-1] + 2 * close[-1]) / 4.0)",
                'currentdate': "kwargs.get('current_date', 0)",
                'currenttime': "kwargs.get('current_time', 0)",
                # Seconds-resolution wall-clock reserved words. Used bare (no
                # parens) they reach this resolver; without an entry they would
                # emit a raw undefined identifier and raise NameError, zeroing
                # the whole trace dict. Resolve to their runtime fns.
                'currenttime_s': "pl_currenttime_s()",
                'currentdate_s': "pl_currentdate_s()",
                'ticks': "kwargs.get('ticks', 0)",
                'upticks': "kwargs.get('upticks', 0)",
                'downticks': "kwargs.get('downticks', 0)",
                'openint': "kwargs.get('openint', 0)",
            }
            # A user-DECLARED name always WINS over a builtin of the same name —
            # including a name declared in a NESTED Variables:/Inputs:/Arrays: block
            # (or defined by a plain assignment) that collides with a bare-computed
            # reserved word (Range/MedianPrice/...) or a __marker__ builtin
            # (FastD/Floor/CCI/...). self._declared_all is the recursive whole-AST
            # declared+assigned oracle; the top-level-only trace/state/array/numeric
            # sets alone MISS a nested declaration, which would then be mis-resolved
            # to the builtin expansion (a discarded decl / SyntaxError / wrong value).
            # An UNDECLARED bare builtin still resolves to its expansion below because
            # user_decl stays False (e.g. `Value1 = Range;` with no Variables: Range).
            user_decl = (name in self._declared_all
                         or name in self._trace_vars or name in self._stateful_vars
                         or name in self._array_vars or name in self._numeric_vars)
            if name in _BARE_COMPUTED and not user_decl:
                return _BARE_COMPUTED[name]
            # EL reserved-word functions can be referenced bare (no parens),
            # e.g. `X = Symbol ;` or `X = Sess1EndTime ;`. Without parens they
            # reach this resolver; emitting the raw identifier would produce
            # undefined-name code that raises NameError at runtime and zeroes
            # the ENTIRE _trace dict (every traced column then falls through to
            # '0'). Route any name with a known builtin mapping through the
            # call generator as a zero-arg call so it resolves to its inline
            # expansion or pl_* runtime fn, exactly like the parenthesized form.
            # Guard: skip user-declared variables (user_decl) — a script may declare
            # a var whose name collides with a builtin (e.g. PrevClose), and it can
            # appear as an assignment TARGET; emitting pl_prevclose() there is a
            # syntax error. User vars always take precedence and pass through.
            if name in BUILTIN_FUNC_MAP and not user_decl:
                return self._gen_call({'name': name, 'args': []})
            # F3: a bare identifier that resolves to nothing — not a declared
            # var/input/array (at ANY depth), not a series/bar keyword, not a
            # recognized reserved word / scratch var (Value*/Condition*), and not
            # a resolvable pl_* builtin — is an UNIMPLEMENTED EL keyword (e.g. a
            # bare `AvgWinTrade`). Emitting the raw lowercased name produces
            # undefined-name code (silent NameError at runtime) and leaves the
            # collect-all report incomplete. Treat it like any other unimplemented
            # keyword: strict collects {name,line} (same dedup/sort/message
            # contract) and raises; partial emits an argless execution-time stub.
            #
            # BUG D: a name the parser recognizes as a reserved identifier passes
            # _is_known_bare_ident, but returning it raw only WORKS when that raw
            # lowercased name actually binds in the generated scope. A reserved
            # word with no pl_ target and no runtime binding (bare `Pi`, an
            # order-syntax word mis-used as a value, a pl_-only keyword) would emit
            # an undefined identifier -> NameError, silently zeroing the trace.
            # Treat it, too, as an unimplemented keyword (fail loud / stub).
            if not self._is_known_bare_ident(name) or not self._bare_raw_resolves(name):
                line = node.get('_line') or self._cur_stmt_line
                self._unimpl_errors.append({'name': name, 'line': line})
                if self._partial:
                    return f"pl_partial_stub({name!r}, {line!r})()"
            return name

        if ntype == 'binop':
            before = len(self._unimpl_errors)
            left = self.gen_expr(node['left'])
            right = self.gen_expr(node['right'])
            op = node['op']
            # BUG A: EL/PL `and`/`or` are NON-short-circuit — BOTH operands are
            # always evaluated. Python's `and`/`or` short-circuit, so a stub on the
            # skipped side (e.g. `(Close < 0) and FakeF(Close)` where the left is
            # False) would be BYPASSED and the expression would silently yield a
            # value instead of raising. Both operands were already walked above (so
            # every nested unimplemented keyword is recorded for the watermark); in
            # PARTIAL mode, if either side collected an unimplemented entry, collapse
            # the whole boolean to a single argless execution-time stub named after
            # the unimplemented operand so it ALWAYS raises when evaluated. Strict
            # mode is unaffected (any recorded entry aborts the whole transpile), so
            # the byte-identical default is preserved.
            if op in ('and', 'or') and self._partial and len(self._unimpl_errors) > before:
                e = self._unimpl_errors[before]
                return f"pl_partial_stub({e['name']!r}, {e['line']!r})()"
            return f"({left} {op} {right})"

        if ntype == 'compare':
            left = self.gen_expr(node['left'])
            right = self.gen_expr(node['right'])
            op = COMPARE_OPS.get(node['op'], node['op'])
            return f"({left} {op} {right})"

        if ntype == 'unaryop':
            operand = self.gen_expr(node['operand'])
            op = node['op']
            if op == 'not':
                return f"(not {operand})"
            return f"({op}{operand})"

        if ntype == 'position_kw':
            kwarg = node['kwarg']
            # Some position keywords have non-zero defaults.
            # AlertEnabled is False during a backtest/data export (alerts only
            # fire on the live last bar), which is the context every GT capture
            # was recorded in — so default it False to match (GTA6's AlEn col).
            defaults = {
                'alert_enabled': 'False',
                'check_alert': 'True',
                'lastbaronchartex': 'False',
            }
            default = defaults.get(kwarg, '0')
            return f"kwargs.get('{kwarg}', {default})"

        if ntype == 'position_kw_call':
            # 2-arg form e.g. PosTradeProfit(PosAgo, TradeNumber).
            # Evaluate args (so referenced names resolve) and look up the
            # per-trade value supplied via kwargs, defaulting to 0.
            kwarg = node['kwarg']
            args = ', '.join(self.gen_expr(a) for a in node.get('args', []))
            return f"pl_pos_trade_field(kwargs, '{kwarg}', {args})"

        if ntype == 'mc_kw':
            kwarg = node['kwarg']
            defaults = {
                'time_s': '0',
                'bartype_ex': '2',
            }
            default = defaults.get(kwarg, '0')
            return f"kwargs.get('{kwarg}', {default})"

        if ntype == 'data_of':
            series = node['series']
            data_num = node['data_num']
            # Emit Data<N> series accessor; falls back to Data1 when Data<N> not provided
            return f"kwargs.get('data{data_num}_{series}', {series}[-1])"

        if ntype == 'call':
            return self._gen_call(node)

        if ntype == 'bar_ref':
            return self._gen_bar_ref(node)

        raise ValueError(f"Unknown expression type: {ntype}")

    # Functions whose arguments should receive full series (not [-1])
    _SERIES_FUNCS = {
        'average', 'averagefc', 'averagetfc', 'weightedaverage', 'xaverage',
        'highest', 'lowest', 'highestbar', 'lowestbar',
        'rsi', 'momentum', 'summation', 'standarddev', 'stddev', 'variance',
        'correlation', 'linearregvalue', 'linearregslope',
        'macdvalue', 'macd', 'macddiff', 'macdsignal',
        'roc', 'rateofchange', 'crossesabove', 'crossesbelow',
        'waverage', 'countif', 'bollingerband', 'stochastic',
        'linearregangle',
    }

    def _is_known_bare_ident(self, name):
        """True if a bare (parenless) identifier reaching gen_expr's ident fallback
        is a LEGITIMATE name and must pass through as-is (never flagged by F3).

        Recognized: any user-declared var/input/array/for name at ANY depth
        (self._declared_all, collected recursively), the codegen's own trace/state
        sets, series params, EL builtins (BUILTIN_FUNC_MAP), any pl_* runtime target,
        the boolean words, and every reserved/scratch identifier the parser's
        semantic oracle (`_is_known_name`) already treats as known — Value0..Value99,
        Condition0..Condition99, marketposition, order words, colors/styles, etc.
        Anything else is an unimplemented EL keyword."""
        if (name in self._declared_all
                or name in self._trace_vars or name in self._stateful_vars
                or name in self._array_vars or name in self._numeric_vars
                or name in BUILTIN_FUNC_MAP
                or name in ('true', 'false')
                or f'pl_{name}' in _available_pl_targets()):
            return True
        from pl_transpiler.parser import _is_known_name
        return _is_known_name(name, self._declared_all)

    def _bare_raw_resolves(self, name):
        """True when a bare identifier reaching the ident fallback can be emitted
        verbatim because the raw lowercased name actually binds in the generated
        strategy() scope: a generated local (user-declared/assigned var, series
        state, array, numeric), an auto-declared scratch var (Value*/Condition*),
        or a function param / prologue internal / runtime-imported name / Python
        builtin (_runtime_bound_bare_names). A reserved word the parser lists but
        which binds to nothing here returns False -> the caller fails it loud
        instead of emitting an undefined name (BUG D)."""
        if (name in self._declared_all
                or name in self._trace_vars or name in self._stateful_vars
                or name in self._array_vars or name in self._numeric_vars):
            return True
        from pl_transpiler.parser import _SCRATCH_VARS
        if name in _SCRATCH_VARS:
            return True
        return name in _runtime_bound_bare_names()

    def _require_pl_impl(self, name, node=None):
        """Validate that a generic `pl_<name>` target resolves in pl_transpiler.builtins.
        On a miss, RECORD the unimplemented keyword (name + nearest source line) into
        the per-generate collection and return the emittable `pl_<name>` target string
        so the walk continues — the whole program is scanned and every unimplemented
        keyword is reported at once at the end of generate(). The emitted text is
        discarded whenever any error was recorded (strict all-or-nothing preserved)."""
        target = f"pl_{name}"
        if target not in _available_pl_targets():
            line = None
            if isinstance(node, dict):
                line = node.get('_line')
            if line is None:
                line = self._cur_stmt_line
            self._unimpl_errors.append({'name': name, 'line': line})
            # FL2 partial mode: emit an execution-time stub callable instead of the
            # generic pl_<name> target. Callers wrap the return in `(args...)`, so
            # `pl_partial_stub('<name>', <line>)` becomes `pl_partial_stub(...)(args)`
            # — a call that RAISES UnimplementedKeywordError only if it is evaluated.
            if self._partial:
                return f"pl_partial_stub({name!r}, {line!r})"
        return target

    def _impl_call(self, name, node, args):
        """Emit the call expression for a builtin target `pl_<name>(args)`.

        Delegates to _require_pl_impl (which records the keyword on a miss). In
        partial mode, when the target is an execution-time stub, the stub is
        invoked with NO arguments — `pl_partial_stub('<name>', <line>)()` (F4:
        the original argument expressions must not survive into the stub call;
        they would evaluate BEFORE the stub raises and can crash with an
        unrelated TypeError). The arg ASTs were already WALKED by the caller
        (building ``args``), so nested unknowns are still collected and the
        watermark stays complete; only the emitted arg strings are dropped."""
        target = self._require_pl_impl(name, node)
        if self._partial and target.startswith('pl_partial_stub('):
            return f"{target}()"
        return f"{target}({', '.join(args)})"

    def _sorted_unimpl(self):
        """Deduplicate + sort the recorded unimplemented keywords: dedup by
        (name, line); sort by (line, name) with None lines last. Returns the list
        of ``{'name','line'}`` dicts shared by the raise path (strict) and the
        watermark/manifest path (partial), so both see the identical ordering."""
        seen = set()
        errors = []
        for e in self._unimpl_errors:
            key = (e['name'], e['line'])
            if key in seen:
                continue
            seen.add(key)
            errors.append({'name': e['name'], 'line': e['line']})
        errors.sort(key=lambda e: (e['line'] is None, e['line'] or 0, e['name']))
        return errors

    def _partial_watermark(self, errors):
        """Build the FL2 partial-mode watermark comment block that PREFIXES the
        generated file when N>0 stubs were emitted. Names every stub (same dedup/
        sort as FL1) and states loudly that the output is not faithful."""
        from pl_transpiler.errors import format_error_lines
        n = len(errors)
        bar = "# " + "=" * 70
        lines = [
            bar,
            "# PARTIAL TRANSPILE — NOT FAITHFUL",
            f"# {n} unimplemented construct(s) replaced with execution-time stubs.",
            "# do not trust backtest results",
            "# Each stub raises UnimplementedKeywordError when evaluated.",
            "# Unimplemented constructs:",
        ]
        for txt in format_error_lines(errors):
            lines.append(f"#   {txt}")
        lines.append(bar)
        return "\n".join(lines) + "\n"

    def _raise_unimplemented(self):
        """Deduplicate + sort the recorded unimplemented keywords and raise ONE
        UnimplementedKeywordError. Dedup by (name, line); sort by (line, name) with
        None lines last. Single-error message is the legacy byte-compatible format;
        N>1 uses the multi-keyword report format."""
        errors = self._sorted_unimpl()

        def _one(e):
            where = f" at line {e['line']}" if e['line'] else ""
            return f"'{e['name']}'{where}"

        if len(errors) == 1:
            e = errors[0]
            where = f" at line {e['line']}" if e['line'] else ""
            message = f"unimplemented EL keyword '{e['name']}'{where}"
        else:
            message = (
                f"cannot transpile: {len(errors)} unimplemented keywords: "
                + "; ".join(_one(e) for e in errors)
            )
        raise UnimplementedKeywordError(message, errors=errors)

    def _gen_call(self, node):
        # FL5 RC1: in PARTIAL mode several inline expansions in _gen_call_impl DROP,
        # RELOCATE, or STRING-INTERPOLATE their argument text — CountIf keeps only
        # args[1]; EntryName/ExitName/BaseDataNumber/CurrentDataNumber/CurrentDate/
        # CurrentTime/Recalculate ignore their args; BarNumberOfData interpolates an
        # arg into a single-quoted f-string. If a nested argument collected an
        # unimplemented keyword (its stub), that stub would then be dropped (the run
        # COMPLETES with a wrong value) or its quotes would break the literal (a
        # compile-time SyntaxError). Collapse the WHOLE call to a single argless
        # `pl_partial_stub('<UNIMPL>', <line>)()` naming an actually-UNIMPLEMENTED
        # keyword collected from the subtree (the first one), so the stub occupies a
        # standalone evaluated position and the run RAISES UnimplementedKeywordError.
        # Only override when the ENCLOSING keyword is itself IMPLEMENTED: when it is
        # unimplemented, _gen_call_impl already returns a correct stub naming IT
        # (result starts with 'pl_partial_stub('), which we keep — a user must never
        # see an implemented keyword reported as unimplemented. The arguments are
        # still fully WALKED inside _gen_call_impl, so every nested unknown is recorded
        # in the watermark. Strict mode is untouched (guarded on self._partial).
        before = len(self._unimpl_errors)
        result = self._gen_call_impl(node)
        if (self._partial and len(self._unimpl_errors) > before
                and not (isinstance(result, str)
                         and result.startswith('pl_partial_stub('))):
            e = self._unimpl_errors[before]
            return f"pl_partial_stub({e['name']!r}, {e['line']!r})()"
        return result

    def _gen_call_impl(self, node):
        name = node['name']
        # Enable series context for functions that accept series args
        old_ctx = self._series_context
        if name in self._SERIES_FUNCS:
            self._series_context = True
        args = [self.gen_expr(a) for a in node['args']]
        self._series_context = old_ctx

        # Special cases
        if name == 'power' and len(args) == 2:
            return f"({args[0]} ** {args[1]})"

        if name in ('log', 'sqrt'):
            self.has_math = True

        py_name = BUILTIN_FUNC_MAP.get(name)

        # Inline expansions for simple math functions
        if py_name == '__intportion__' and len(args) == 1:
            return f"int({args[0]})"
        if py_name == '__fracportion__' and len(args) == 1:
            return f"({args[0]} - int({args[0]}))"
        if py_name == '__sign__' and len(args) == 1:
            a = args[0]
            return f"(1 if {a} > 0 else (-1 if {a} < 0 else 0))"
        if py_name == '__square__' and len(args) == 1:
            return f"({args[0]} ** 2)"
        if py_name == '__numtostr__' and len(args) >= 2:
            return f'f"{{{args[0]}:.{{{args[1]}}}f}}"'
        if py_name == '__strtonum__' and len(args) == 1:
            return f"float({args[0]})"

        # Technical indicators that need high/low/close injected
        if py_name == '__cci__' and len(args) == 1:
            return f"pl_cci(high, low, close, {args[0]})"
        if py_name == '__atr__' and len(args) == 1:
            return f"pl_atr(high, low, close, {args[0]})"
        if py_name == '__adx__' and len(args) == 1:
            return f"pl_adx(high, low, close, {args[0]})"
        if py_name == '__adxr__' and len(args) == 1:
            return f"pl_adxr(high, low, close, {args[0]})"
        if py_name == '__dmiplus__' and len(args) == 1:
            return f"pl_dmiplus(high, low, close, {args[0]})"
        if py_name == '__dmiminus__' and len(args) == 1:
            return f"pl_dmiminus(high, low, close, {args[0]})"
        # CountIf: count where condition is true over window
        if py_name == '__countif__':
            return f"pl_countif_window(close, open, {args[1]})"
        # Stochastic: multi-output var-parameter
        if py_name == '__stochastic__':
            # Stochastic(High, Low, Close, Length, SmoothK, SmoothD, SmoothType, oFastK, oFastD, oSlowK, oSlowD)
            # The output vars (oFastK, oFastD, oSlowK, oSlowD) are set by the function
            return f"pl_stochastic({', '.join(args)})"
        if py_name == '__fastk__' and len(args) == 1:
            return f"pl_fast_k(high, low, close, {args[0]})"
        if py_name == '__fastd__' and len(args) == 2:
            return f"pl_fast_d(high, low, close, {args[0]}, {args[1]})"
        if py_name == '__slowk__' and len(args) == 2:
            return f"pl_slow_k(high, low, close, {args[0]}, {args[1]})"
        if py_name == '__slowd__' and len(args) == 3:
            return f"pl_slow_d(high, low, close, {args[0]}, {args[1]}, {args[2]})"

        # Math functions
        if py_name == '__ceiling__' and len(args) == 1:
            self.has_math = True
            return f"math.ceil({args[0]})"
        if py_name == '__floor__' and len(args) == 1:
            self.has_math = True
            return f"math.floor({args[0]})"
        if py_name == '__round__' and len(args) == 1:
            self._need_f32_import = True
            return f"el_round0({args[0]})"
        if py_name == '__round__' and len(args) == 2:
            self._need_f32_import = True
            return f"el_round({args[0]}, {args[1]})"
        if py_name == '__expvalue__' and len(args) == 1:
            self.has_math = True
            return f"math.exp({args[0]})"

        # String functions
        if py_name == '__leftstr__' and len(args) == 2:
            return f"{args[0]}[:{args[1]}]"
        if py_name == '__rightstr__' and len(args) == 2:
            return f"{args[0]}[-{args[1]}:]"
        if py_name == '__midstr__' and len(args) == 3:
            # EL is 1-indexed: MidStr("abcdef",2,3)="bcd" → Python "abcdef"[1:4]
            return f"{args[0]}[({args[1]} - 1):({args[1]} - 1) + {args[2]}]"
        if py_name == '__strlen__' and len(args) == 1:
            return f"__builtin_len({args[0]})"
        if py_name == '__strcontains__' and len(args) == 2:
            return f"({args[1]} in {args[0]})"
        if py_name == '__strreplace__' and len(args) == 3:
            return f"{args[0]}.replace({args[1]}, {args[2]})"
        if py_name == '__strfind__' and len(args) == 2:
            return f"{args[0]}.find({args[1]})"
        if py_name == '__lowerstr__' and len(args) == 1:
            return f"{args[0]}.lower()"
        if py_name == '__upperstr__' and len(args) == 1:
            return f"{args[0]}.upper()"
        if py_name == '__stringformat__' and len(args) >= 1:
            fmt_args = ', '.join(args[1:])
            return f"{args[0]}.format({fmt_args})"

        # Date/time functions
        if py_name == '__hour__' and len(args) == 1:
            return f"({args[0]} // 100)"
        if py_name == '__minute__' and len(args) == 1:
            return f"({args[0]} % 100)"
        if py_name == '__second__' and len(args) == 1:
            return f"({args[0]} % 100)"

        # Output functions
        if py_name == '__plotpb__' and len(args) == 4:
            return f"_plots.setdefault('plotpb', []).append(({args[0]}, {args[1]}, {args[2]}, {args[3]}))"
        if py_name == '__printlog__':
            return f"print({', '.join(args)})"
        if py_name == '__setplotcolor__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_color', {args[1]})"
        if py_name == '__setplotwidth__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_width', {args[1]})"
        if py_name == '__setplotstyle__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_style', {args[1]})"

        # MC extensions
        if py_name == '__basedatanumber__':
            return "kwargs.get('base_data_number', 1)"
        if py_name == '__currentdatanumber__':
            return "kwargs.get('current_data_number', 1)"
        if py_name == '__recalculate__':
            return "kwargs.__setitem__('_needs_recalc', True)"
        if py_name == '__setstopposition__' and len(args) == 1:
            return f"_risk.__setitem__('stop_position', {args[0]})"
        if py_name == '__barnumberofdata__' and len(args) == 1:
            return f"kwargs.get('bar_number_of_data_{{{args[0]}}}', 0)"

        # Date/time no-arg functions
        if py_name == '__currentdate__':
            return "kwargs.get('current_date', 0)"
        if py_name == '__currenttime__':
            return "kwargs.get('current_time', 0)"
        if py_name == '__timetostring__' and len(args) == 1:
            return f"pl_timetostring({args[0]})"
        if py_name == '__datetostring__' and len(args) == 1:
            return f"pl_datetostring({args[0]})"
        if py_name == '__eldatetodatetime__' and len(args) == 1:
            # EL ELDateToDateTime(ELDate): convert an EL date (YYYMMDD,
            # years-since-1900) to a MC DateTime serial (days since 1899-12-30,
            # Excel-style). Identical day-serial base as DateToJulian.
            return f"pl_datetojulian({args[0]})"
        if py_name == '__eldatetodatetime__' and len(args) == 2:
            return f"({args[0]} * 10000 + {args[1]})"
        if py_name == '__eltimetodatetime__' and len(args) == 1:
            # ELTimeToDateTime(HHmm) -> time-of-day fraction (pdf:4499
            # ELTimeToDateTime(1015)=0.42708333).
            return f"(((int({args[0]}) // 100) * 3600 + (int({args[0]}) % 100) * 60) / 86400.0)"
        if py_name == '__datetimetoeldate__' and len(args) == 1:
            return f"int({args[0]} // 10000)"
        if py_name == '__datetimetoeltime__' and len(args) == 1:
            return f"int({args[0]} % 10000)"

        # Period data functions (OpenD, HighD, etc.)
        if py_name == '__period_data__':
            offset = int(args[0]) if args else 0
            return f"kwargs.get('{name}_{offset}', 0)"

        # Output extensions
        if py_name == '__setplotbgcolor__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_bgcolor', {args[1]})"
        if py_name == '__noplot__' and len(args) == 1:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_noplot', True)"

        # Order extensions
        if py_name == '__setbreakeven__' and len(args) == 1:
            return f"_risk.__setitem__('breakeven', {args[0]})"
        if py_name == '__entryname__' and len(args) >= 1:
            return f"kwargs.get('entry_name', '')"
        if py_name == '__exitname__' and len(args) >= 1:
            return f"kwargs.get('exit_name', '')"

        # CurrentSession: allow runner to override via kwarg (runner computes from bar date/time).
        if py_name == 'pl_currentsession':
            return f"kwargs.get('currentsession', pl_currentsession({', '.join(args)}))"

        # SessionLastBar: runner derives "last bar of the trading session" from the bar
        # date series (next bar is a new day) and supplies it via 'session_last_bar',
        # mirroring CurrentSession. Not capture-fed — derived from the date series.
        if py_name == 'pl_sessionlastbar':
            return f"kwargs.get('session_last_bar', pl_sessionlastbar({', '.join(args)}))"

        # LastCalc*: date/time of the FINAL bar of the data series (the moment of the
        # last calculation in a historical export). The runner derives these from the
        # last bar's date/time series (fundamental inputs, NOT the lc* output columns),
        # mirroring CurrentSession/SessionLastBar. Constant across all bars.
        _LASTCALC_KW = {
            'pl_lastcalcdatetime': 'lastcalcdatetime',
            'pl_lastcalcjdate': 'lastcalcjdate',
            'pl_lastcalcmmtime': 'lastcalcmmtime',
            'pl_lastcalcmstime': 'lastcalcmstime',
            'pl_lastcalcsstime': 'lastcalcsstime',
        }
        if py_name in _LASTCALC_KW:
            return f"kwargs.get('{_LASTCALC_KW[py_name]}', {py_name}({', '.join(args)}))"

        # AUDIT 2026-06-19: removed the _KWARGS_PREFER hook for session/config funcs — it
        # existed only to consume capture-fed kwargs (circular self-feed). Session/contract-spec
        # values must come from honest runtime impls or known chart config, not the capture.
        if py_name and not py_name.startswith('__'):
            return f"{py_name}({', '.join(args)})"

        if py_name:
            # A BUILTIN_FUNC_MAP keyword whose internal `__marker__` py_name had no
            # faithful inline expansion here (e.g. wrong arg count, or a bare
            # zero-arg reference like `Value1 = FastD;`). Emitting the marker
            # (`__fastd__()`) would be an undefined name — strict returns broken
            # output and partial stamps a runnable header on unrunnable code. Route
            # through the unimplemented path instead (recording the EL name, not the
            # marker): strict collects+raises, partial emits an argless stub.
            return self._impl_call(name, node, args)

        # Unknown function: use pl_ prefix (validated to be implemented in builtins)
        return self._impl_call(name, node, args)

    def _gen_bar_ref(self, node):
        # User variables access _state history list; data series access the list directly.
        # EL: VarName[n] = value n bars ago. User var state list stores end-of-bar values,
        # so VarName[n] = _state['name'][-n] (not -(n+1) like data series).
        series = node['series']
        index = node['index']

        # Position-info function call: MaxPositionProfit(1), MaxContracts(1), etc.
        # The parser models these as bar_ref(series=position_kw, index=PosAgo).
        # The runtime supplies them as a SCALAR kwarg (no positions-ago history),
        # so subscripting it (`kwargs.get(...)[-2]`) raises 'int not subscriptable'
        # and aborts the whole bar — zeroing every traced column. Return the
        # scalar value directly, discarding the positions-ago index.
        #
        # BUG B: the positions-ago INDEX must still be WALKED so a nested
        # unimplemented keyword inside it is recorded (e.g. MaxContracts(FakeF(Close))
        # / EntryPrice(FakeF(Close))). Previously the index was never visited, so
        # the unknown was silently accepted (strict emitted kwargs.get(...) and
        # partial reported '0 unimplemented constructs' — a watermark lie). Its
        # emitted value is discarded (the kwarg is a scalar with no history), but
        # its subtree is scanned exactly like any other arg; in partial mode, if the
        # index collected an unimplemented entry, emit the argless stub so it raises.
        if series['type'] == 'position_kw':
            before = len(self._unimpl_errors)
            self.gen_expr(index)
            if self._partial and len(self._unimpl_errors) > before:
                e = self._unimpl_errors[before]
                return f"pl_partial_stub({e['name']!r}, {e['line']!r})()"
            return self.gen_expr(series)

        if series['type'] == 'ident' and series['name'] in self._stateful_vars and self.trace:
            name = series['name']
            if index['type'] == 'number':
                n = int(index['value'])
                if n == 0:
                    return name  # VarName[0] = current value
                # Safe access: if not enough history, return first (initial) value
                # Use __builtin_len because 'len' may be shadowed by an EL input
                return f"(_state['{name}'][-{n}] if __builtin_len(_state['{name}']) >= {n} else _state['{name}'][0])"
            idx_expr = self.gen_expr(index)
            idx_int = f"int({idx_expr})"
            return f"(_state['{name}'][-{idx_int}] if __builtin_len(_state['{name}']) >= {idx_expr} else _state['{name}'][0])"

        # Function-call historical reference: Highest(High, Len)[1]
        if series['type'] == 'call':
            # For func(args...)[n], compute the function on data from n bars ago
            # by slicing the data series arguments.
            func_name = series['name']
            old_ctx = self._series_context
            if func_name in self._SERIES_FUNCS:
                self._series_context = True
            raw_args = [self.gen_expr(a) for a in series['args']]
            self._series_context = old_ctx
            
            if index['type'] == 'number':
                offset = int(index['value'])  # EL 1-indexed → remove N trailing elements
            else:
                offset_expr = self.gen_expr(index)
                offset = f"({offset_expr})"
            
            # Inject high/low/close for indicators that need them (__adx__, __cci__, etc.)
            py_name = BUILTIN_FUNC_MAP.get(func_name)
            if py_name == '__adx__' and len(raw_args) == 1:
                return f"pl_adx(high[:-{offset}], low[:-{offset}], close[:-{offset}], {raw_args[0]})" if isinstance(offset, int) else f"pl_adx(high[:-{offset}], low[:-{offset}], close[:-{offset}], {raw_args[0]})"
            if py_name == '__adxr__' and len(raw_args) == 1:
                return f"pl_adxr(high[:-{offset}], low[:-{offset}], close[:-{offset}], {raw_args[0]})"
            if py_name == '__dmiplus__' and len(raw_args) == 1:
                return f"pl_dmiplus(high[:-{offset}], low[:-{offset}], close[:-{offset}], {raw_args[0]})"
            if py_name == '__dmiminus__' and len(raw_args) == 1:
                return f"pl_dmiminus(high[:-{offset}], low[:-{offset}], close[:-{offset}], {raw_args[0]})"
            
            # For regular functions, pass series args with offset slicing
            # Slice each data series argument to shift the lookback window
            sliced_args = []
            for i, arg in enumerate(series['args']):
                if arg['type'] == 'ident' and arg['name'] in _SERIES_PARAMS:
                    s = raw_args[i]
                    if isinstance(offset, int):
                        sliced_args.append(f"{s}[:-{offset}]" if offset > 0 else s)
                    else:
                        sliced_args.append(f"{s}[:-{offset}]" if offset else s)
                else:
                    sliced_args.append(raw_args[i])
            
            if py_name and not py_name.startswith('__'):
                return f"{py_name}({', '.join(sliced_args)})"
            return self._impl_call(func_name, series, sliced_args)

        # Original behaviour for data series and arrays
        old_ctx = self._series_context
        self._series_context = True
        series_expr = self.gen_expr(series)
        self._series_context = old_ctx

        if index['type'] == 'number':
            n_str = index['value']
            n = int(n_str)
            if n > 0 and series_expr in ('close', 'high', 'low', 'open', 'volume', 'ticks', 'upticks', 'downticks'):
                # EL: on first bar(s), historical refs fall back to current bar's Open
                # Runner pads 1 element; check if index hits padding region
                return f"({series_expr}[-({n_str} + 1)] if __builtin_len({series_expr}) > 1 + {n} else open[-1])"
            return f"{series_expr}[-({n_str} + 1)]"

        idx_expr = self.gen_expr(index)
        idx_int = f"int({idx_expr})"
        if series_expr in ('close', 'high', 'low', 'open', 'volume', 'ticks', 'upticks', 'downticks'):
            return f"({series_expr}[-({idx_int} + 1)] if __builtin_len({series_expr}) > 1 + ({idx_expr}) else open[-1])"
        return f"{series_expr}[-({idx_int} + 1)]"


def generate(ast, trace=False, partial=False):
    """Walk AST and emit Python source string.
    If trace=True, generated code captures per-bar variable values into _trace dict.
    If partial=True (FL2 opt-in), unimplemented EL keywords become execution-time
    stubs (pl_partial_stub) under a NOT-FAITHFUL watermark instead of raising at
    transpile time; the strict default (partial=False) is unchanged."""
    cg = CodeGen(trace=trace, partial=partial)
    return cg.gen_program(ast)
