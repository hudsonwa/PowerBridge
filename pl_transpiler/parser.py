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

"""PowerLanguage Parser — builds an AST from token list via recursive descent."""

import re

from .lexer import (
    TT_KEYWORD, TT_IDENT, TT_NUMBER, TT_STRING, TT_OP,
    TT_LBRACKET, TT_RBRACKET, TT_LPAREN, TT_RPAREN,
    TT_SEMICOLON, TT_COMMA, TT_EOF,
    PLSyntaxError, _source_line, collect_comments,
)

# Order-related keywords
ORDER_ACTIONS = {'buy', 'sell', 'sellshort', 'buytocover'}
RISK_FUNCS = {'setstoploss', 'setprofittarget', 'setexitonclose', 'setdollartrailing', 'setpercenttrailing', 'setstopposition', 'setbreakeven', 'setstopcontract', 'setstopshare', 'setstopposition'}
PLOT_NAMES = {f'plot{i}' for i in range(1, 10)} | {'plotpb'}

# Built-in data series
DATA_SERIES = {'close', 'open', 'high', 'low', 'volume', 'date', 'time', 'time_s',
               'ticks', 'upticks', 'downticks', 'openint', 'barnumber', 'currentbar',
               'lastbaronchart', 'barstatus', 'bartype', 'barinterval',
               'sessionnumber', 'dayofweek', 'dayofmonth', 'month', 'year'}

# Color constants → integer values
COLOR_CONSTANTS = {
    'black': 0x000000, 'white': 0xFFFFFF, 'red': 0xFF0000, 'green': 0x00FF00,
    'blue': 0x0000FF, 'yellow': 0xFFFF00, 'cyan': 0x00FFFF, 'magenta': 0xFF00FF,
    'darkred': 0x800000, 'darkgreen': 0x008000, 'darkblue': 0x000080,
    'darkcyan': 0x008080, 'darkmagenta': 0x800080, 'darkyellow': 0x808000,
    'gray': 0x808080, 'darkgray': 0x404040, 'lightgray': 0xC0C0C0,
}

# Style constants
STYLE_CONSTANTS = {
    'tool_solid': 0, 'tool_dashed': 1, 'tool_dotted': 2, 'tool_dashdot': 3,
}

# Functions that take periods-ago arg: FuncD(0), FuncW(0), FuncM(0)
PERIOD_FUNCS = {
    'opend', 'highd', 'lowd', 'closed', 'volumed',
    'openw', 'highw', 'loww', 'closew',
    'openm', 'highm', 'lowm', 'closem',
}

# Curated argument-count table for common EL builtins, used by the post-parse
# semantic pass to catch wrong-arity calls (the #1 silent-failure class the LLM
# workflow hits, e.g. Average(Close) missing its length). Each value is the set
# of accepted argument counts. Functions NOT in this table are not arity-checked
# (we only flag what we are confident about, to avoid false positives).
BUILTIN_ARITY = {
    'average': {2}, 'averagefc': {2}, 'averagetfc': {2},
    'xaverage': {2}, 'weightedaverage': {2}, 'waverage': {2},
    'highest': {2}, 'lowest': {2}, 'highestbar': {2}, 'lowestbar': {2},
    'summation': {2}, 'momentum': {2}, 'roc': {2}, 'rateofchange': {2},
    'rsi': {2},
    # StdDev/Variance take (Price, Length, DataType) in EL — 3 args.
    'stddev': {3}, 'standarddev': {3}, 'variance': {3},
    'correlation': {3},
    'crossesabove': {2}, 'crossesbelow': {2},
    'absvalue': {1}, 'sign': {1}, 'square': {1}, 'squareroot': {1}, 'sqrt': {1},
    'intportion': {1}, 'fracportion': {1}, 'ceiling': {1}, 'floor': {1},
    'log': {1}, 'expvalue': {1},
    'power': {2}, 'round': {2},
    'truerange': {0},
    'cci': {1}, 'atr': {1}, 'avgtruerange': {1},
    # NOTE: MaxList/MinList are variadic in EL (2+ args) — deliberately NOT
    # arity-checked to avoid false positives on valid variadic calls.
}

# Known PL built-in functions mapped to Python equivalents
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
    'absvalue': '__abs__',
    'maxlist': '__max__',
    'minlist': '__min__',
    'log': '__log__',
    'sqrt': '__sqrt__',
    'power': '__power__',
    'truerange': 'pl_true_range',
    'intportion': '__intportion__',
    'fracportion': '__fracportion__',
    'sign': '__sign__',
    'square': '__square__',
    'numtostr': '__numtostr__',
    'strtonum': '__strtonum__',
    'standarddev': 'pl_std_dev',
    # Technical indicators
    'macd': 'pl_macd',
    'macdvalue': 'pl_macd',
    'macddiff': 'pl_macd_diff',
    'macdsignal': 'pl_macd_signal',
    'roc': 'pl_roc',
    'cci': 'pl_cci',
    'atr': 'pl_atr',
    'avgtruerange': 'pl_atr',
    'fastk': 'pl_fast_k',
    'fastd': 'pl_fast_d',
    'slowk': 'pl_slow_k',
    'slowd': 'pl_slow_d',
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
    # Output
    'plotpb': '__plotpb__',
    'printlog': '__printlog__',
    'setplotcolor': '__setplotcolor__',
    'setplotwidth': '__setplotwidth__',
    'setplotstyle': '__setplotstyle__',
    # MC extensions
    'recalculate': '__recalculate__',
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
    'calcdate': 'pl_calcdate',
    'calctime': 'pl_calctime',
    'dayofweek': 'pl_dayofweek',
    'iff': 'pl_iff',
    'currentsession': 'pl_currentsession',
    'barinterval': 'pl_barinterval',
    'setdollartrailing': '__setdollartrailing__',
    'currentdate': '__currentdate__',
    'currenttime': '__currenttime__',
    'timetostring': '__timetostring__',
    'datetostring': '__datetostring__',
    'eldatetodatetime': 'eldatetodatetime',
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


def _reconstruct_tokens(toks):
    """Best-effort text reconstruction of a consumed token run, used to retain
    attribute arg lists (`[Name(args)]`) in the AST. EL is case- and
    whitespace-insensitive, so this is faithful enough for the reverse emitter
    to reproduce the attribute."""
    out = []
    for i, (tt, val) in enumerate(toks):
        if out and tt not in (TT_COMMA, TT_RPAREN, TT_RBRACKET, TT_LPAREN) \
                and toks[i - 1][0] not in (TT_LPAREN, TT_LBRACKET):
            out.append(' ')
        out.append(str(val))
    return ''.join(out)


class Parser:
    def __init__(self, tokens, positions=None, source=''):
        self.tokens = tokens
        self.positions = positions or []
        self.source = source
        self.pos = 0
        # Character offset of the start of each 1-based source line, so a token's
        # (line, col) position can be mapped back to the verbatim source text of an
        # identifier (the lexer lowercases idents; the ORIGINAL spelling is recovered
        # here per-occurrence for the '_ident' side-channel — see _spelling_at).
        self._line_starts = None

    def _spelling_at(self, tok_index):
        """The verbatim source spelling of the identifier token at `tok_index`, or
        None when source/positions are unavailable. Reads the original (non-lowered)
        text straight from `self.source` at the token's recorded (line, col) — the
        lexer lowercases identifiers, so this is the ONLY way to recover the case the
        author wrote. Per-occurrence, so each use round-trips its own spelling."""
        if not self.source or not self.positions:
            return None
        line, col = self._pos_of(tok_index)
        if line <= 0 or col <= 0:
            return None
        if self._line_starts is None:
            starts, off = [0], 0
            for ln in self.source.split('\n'):
                off += len(ln) + 1
                starts.append(off)
            self._line_starts = starts
        if line > len(self._line_starts):
            return None
        off = self._line_starts[line - 1] + (col - 1)
        m = re.match(r'[A-Za-z_#][A-Za-z0-9_.]*', self.source[off:])
        return m.group() if m else None

    def _ident_spelling(self, name, tok_index):
        """The original spelling to stamp under '_ident' for a USER identifier token,
        or None if it should not be stamped: builtins/keywords keep their canonical
        casing (tools/pl_signatures.jsonl) so they are excluded, and an already-
        lowercase spelling (== the parser's `name`) is redundant and omitted. Keeping
        this to genuine, differently-cased user identifiers means the forward output is
        byte-identical (codegen ignores '_ident') and the reparse-fixpoint holds."""
        if _is_known_name(name, frozenset()):
            return None
        spelling = self._spelling_at(tok_index)
        if spelling is None or spelling == name:
            return None
        return spelling

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (TT_EOF, '')

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _pos_of(self, tok_index):
        """(line, col) for the token at tok_index (clamped to the position list)."""
        if not self.positions:
            return (0, 0)
        idx = min(max(tok_index, 0), len(self.positions) - 1)
        return self.positions[idx]

    def _err(self, message, hint='', tok_index=None):
        """Raise a PLSyntaxError located at the given (default: current) token."""
        if tok_index is None:
            tok_index = self.pos
        line, col = self._pos_of(tok_index)
        raise PLSyntaxError(message, line, col,
                            _source_line(self.source, line), hint)

    @staticmethod
    def _describe(tok):
        """Human-readable description of a token for error messages."""
        tt, val = tok
        if tt == TT_EOF:
            return 'end of input'
        if tt in (TT_KEYWORD, TT_IDENT):
            return f"'{val}'"
        if val:
            return f"'{val}'"
        return tt

    def expect(self, tt, val=None):
        tok = self.peek()
        if tok[0] != tt or (val is not None and tok[1] != val):
            want = repr(val) if val is not None else tt
            self._err(
                f"expected {want} but found {self._describe(tok)}",
                hint=f"insert {want} here" if val is not None else '')
        return self.advance()

    def match(self, tt, val=None):
        tok = self.peek()
        if tok[0] == tt and (val is None or tok[1] == val):
            return self.advance()
        return None

    def at_end(self):
        return self.peek()[0] == TT_EOF

    # ---- Program ----
    def parse_program(self):
        stmts = []
        while not self.at_end():
            # skip stray semicolons
            if self.match(TT_SEMICOLON):
                continue
            stmt = self.parse_statement()
            if stmt:
                stmts.append(stmt)
        return {'type': 'program', 'body': stmts}

    # ---- Statement ----
    def parse_statement(self):
        # Stamp the 1-based source line of the statement's first token on the
        # returned node under the `_line` key (additive statement-line metadata;
        # the reverse emitter uses gaps between consecutive statements' `_line` to
        # preserve blank-line structure on round-trip).
        #
        # NOTE on the key name: the Rfmt brief calls this `_stmt_line`, but a
        # *new* diverging key would redden `verify_roundtrip.py --fixpoint`
        # (parse(src) and parse(emit_el(...)) land statements on different absolute
        # lines, so any line-valued key that survives the fixpoint's `_strip`
        # diverges) — and Rfmt has no guard allowance to add it to that gate's
        # ignore set (deferred to Rcmt, which does). `_line` is ALREADY in the
        # fixpoint + similarity ignore sets and already means "the source line of
        # this node", so statement-line metadata rides it fixpoint-safely with no
        # gate edit. Forward codegen ignores it (verify_all --fast proves output
        # unchanged). py_front-synthesized ASTs carry no `_line`, so the emitter
        # falls back to section-based blank lines there.
        line, _col = self._pos_of(self.pos)
        node = self._parse_statement_dispatch()
        if isinstance(node, dict) and line and '_line' not in node:
            node['_line'] = line
        return node

    def _parse_statement_dispatch(self):
        tok = self.peek()
        tt, val = tok

        # Variable declarations
        if tt == TT_KEYWORD and val in ('variables', 'variable', 'vars', 'var'):
            return self.parse_var_decl()

        # Input declarations
        if tt == TT_KEYWORD and val in ('inputs', 'input'):
            return self.parse_input_decl()

        # If statement
        if tt == TT_KEYWORD and val == 'if':
            return self.parse_if()

        # For loop
        if tt == TT_KEYWORD and val == 'for':
            return self.parse_for()

        # While loop
        if tt == TT_KEYWORD and val == 'while':
            return self.parse_while()

        # Repeat..Until loop
        if tt == TT_KEYWORD and val == 'repeat':
            return self.parse_repeat_until()

        # Switch/case
        if tt == TT_KEYWORD and val == 'switch':
            return self.parse_switch()

        # Array declarations
        if tt == TT_KEYWORD and val in ('arrays', 'array'):
            return self.parse_array_decl()

        # Strategy/study attribute declaration: [Name = Value] or [Name(args)].
        # A '[' at statement start is an attribute (array indexing only appears
        # inside expressions, after an identifier).
        if tt == TT_LBRACKET:
            return self.parse_attribute()

        # Once block
        if tt == TT_KEYWORD and val == 'once':
            return self.parse_once()

        # Break
        if tt == TT_KEYWORD and val == 'break':
            self.advance()
            self.match(TT_SEMICOLON)
            return {'type': 'break'}

        # Continue
        if tt == TT_KEYWORD and val == 'continue':
            self.advance()
            self.match(TT_SEMICOLON)
            return {'type': 'continue'}

        # Abort (EL: aborts the calculation at run-time). Statement form, no args.
        if tt == TT_KEYWORD and val == 'abort':
            self.advance()
            self.match(TT_SEMICOLON)
            return {'type': 'abort'}

        # Order actions (lexer emits these as IDENT)
        if tt == TT_IDENT and val in ORDER_ACTIONS:
            return self.parse_order()

        # Risk functions
        if tt == TT_IDENT and val in RISK_FUNCS:
            return self.parse_risk_func()

        # Plot
        if tt == TT_IDENT and val in PLOT_NAMES:
            return self.parse_plot()

        # Alert
        if tt == TT_IDENT and val == 'alert':
            return self.parse_alert()

        # Commentary
        if tt == TT_IDENT and val == 'commentary':
            return self.parse_commentary()

        # Print / PrintLog
        if tt == TT_IDENT and val in ('print', 'printlog'):
            return self.parse_print_stmt()

        # Assignment or expression statement
        return self.parse_assignment_or_expr()

    # ---- Variable Declaration ----
    def parse_var_decl(self):
        self.advance()  # consume 'variables'/'var'
        self.match(TT_OP, ':')  # optional colon
        decls = []
        while True:
            # IntraBarPersist modifier: retain per-decl (semantic for reverse emit;
            # codegen ignores the flag so forward output is unchanged).
            intrabar = self.match(TT_KEYWORD, 'intrabarpersist') is not None
            name_index = self.pos
            name = self.expect(TT_IDENT)[1]
            ident = self._ident_spelling(name, name_index)
            init = {'type': 'number', 'value': '0'}
            data_ref = None
            if self.match(TT_LPAREN):
                init = self.parse_expression()
                # Data2-tied: Varname( expr, DataN )
                if self.match(TT_COMMA):
                    data_ref = self.parse_expression()
                self.expect(TT_RPAREN)
            decl = {'name': name, 'init': init, 'data_ref': data_ref}
            if intrabar:
                decl['intrabar_persist'] = True
            if ident is not None:
                decl['_ident'] = ident
            decls.append(decl)
            if not self.match(TT_COMMA):
                break
        self.match(TT_SEMICOLON)
        return {'type': 'var_decl', 'decls': decls}

    # ---- Input Declaration ----
    def parse_input_decl(self):
        self.advance()  # consume 'inputs'
        self.match(TT_OP, ':')
        decls = []
        while True:
            name_index = self.pos
            name = self.expect(TT_IDENT)[1]
            ident = self._ident_spelling(name, name_index)
            default = {'type': 'number', 'value': '0'}
            if self.match(TT_LPAREN):
                default = self.parse_expression()
                self.expect(TT_RPAREN)
            decl = {'name': name, 'default': default}
            if ident is not None:
                decl['_ident'] = ident
            decls.append(decl)
            if not self.match(TT_COMMA):
                break
        self.match(TT_SEMICOLON)
        return {'type': 'input_decl', 'decls': decls}

    # ---- If Statement ----
    def parse_if(self):
        self.expect(TT_KEYWORD, 'if')
        cond = self.parse_expression()
        self.expect(TT_KEYWORD, 'then')

        # Check for begin/end block. Whether the source wrote an explicit Begin/End
        # (vs a bare single statement) is retained additively per leg so the reverse
        # emitter can reproduce the compact `If C Then <stmt>` form; codegen ignores
        # the flag (forward output unchanged).
        then_block = self.peek() == (TT_KEYWORD, 'begin')
        if then_block:
            body = self.parse_block()
        else:
            stmt = self.parse_statement()
            body = [stmt] if stmt else []

        # Else clause
        else_body = None
        else_block = False
        if self.match(TT_KEYWORD, 'else'):
            else_block = self.peek() == (TT_KEYWORD, 'begin')
            if else_block:
                else_body = self.parse_block()
            else:
                stmt = self.parse_statement()
                else_body = [stmt] if stmt else []

        node = {'type': 'if', 'cond': cond, 'body': body, 'else_body': else_body}
        if then_block:
            node['then_block'] = True
        if else_block:
            node['else_block'] = True
        return node

    # ---- Block ----
    def parse_block(self):
        begin_index = self.pos
        self.expect(TT_KEYWORD, 'begin')
        stmts = []
        while self.peek() != (TT_KEYWORD, 'end') and not self.at_end():
            if self.match(TT_SEMICOLON):
                continue
            stmt = self.parse_statement()
            if stmt:
                stmts.append(stmt)
        if self.peek() != (TT_KEYWORD, 'end'):
            # Reached EOF (or stray token) without closing the block — point at
            # the unclosed 'begin' so the LLM knows which block needs an 'end'.
            self._err("expected 'end' to close 'begin'",
                      hint="add an 'end;' to close this 'begin' block",
                      tok_index=begin_index)
        self.expect(TT_KEYWORD, 'end')
        self.match(TT_SEMICOLON)
        return stmts

    # ---- For Loop ----
    def parse_for(self):
        self.expect(TT_KEYWORD, 'for')
        var_index = self.pos
        var = self.expect(TT_IDENT)[1]
        var_ident = self._ident_spelling(var, var_index)
        self.expect(TT_OP, '=')
        start = self.parse_expression()

        downto = False
        if self.match(TT_KEYWORD, 'to'):
            downto = False
        elif self.match(TT_KEYWORD, 'downto'):
            downto = True
        else:
            self._err(
                f"expected 'to' or 'downto' but found {self._describe(self.peek())}",
                hint="a for-loop reads: for i = start to end begin ... end")

        end = self.parse_expression()

        block = self.peek() == (TT_KEYWORD, 'begin')
        if block:
            body = self.parse_block()
        else:
            stmt = self.parse_statement()
            body = [stmt] if stmt else []

        node = {'type': 'for', 'var': var, 'start': start, 'end': end,
                'downto': downto, 'body': body}
        if block:
            node['block'] = True
        if var_ident is not None:
            node['_var_ident'] = var_ident
        return node

    # ---- While Loop ----
    def parse_while(self):
        self.expect(TT_KEYWORD, 'while')
        cond = self.parse_expression()
        block = self.peek() == (TT_KEYWORD, 'begin')
        if block:
            body = self.parse_block()
        else:
            stmt = self.parse_statement()
            body = [stmt] if stmt else []
        node = {'type': 'while', 'cond': cond, 'body': body}
        if block:
            node['block'] = True
        return node

    # ---- Repeat..Until ----
    def parse_repeat_until(self):
        self.expect(TT_KEYWORD, 'repeat')
        body = []
        while self.peek() != (TT_KEYWORD, 'until') and not self.at_end():
            stmt = self.parse_statement()
            if stmt:
                body.append(stmt)
        self.expect(TT_KEYWORD, 'until')
        cond = self.parse_expression()
        self.match(TT_SEMICOLON)
        return {'type': 'repeat_until', 'body': body, 'cond': cond}

    # ---- Once ----
    def parse_once(self):
        self.expect(TT_KEYWORD, 'once')
        block = self.peek() == (TT_KEYWORD, 'begin')
        if block:
            body = self.parse_block()
        else:
            stmt = self.parse_statement()
            body = [stmt] if stmt else []
        node = {'type': 'once', 'body': body}
        if block:
            node['block'] = True
        return node

    # ---- Switch/Case ----
    def parse_switch(self):
        self.expect(TT_KEYWORD, 'switch')
        # Optional parens around expression
        has_paren = bool(self.match(TT_LPAREN))
        expr = self.parse_expression()
        if has_paren:
            self.expect(TT_RPAREN)
        self.expect(TT_KEYWORD, 'begin')
        cases = []
        default_body = None
        while self.peek() != (TT_KEYWORD, 'end') and not self.at_end():
            if self.match(TT_SEMICOLON):
                continue
            if self.match(TT_KEYWORD, 'case'):
                case_val = self.parse_expression()
                self.expect(TT_OP, ':')
                body = []
                while (self.peek() != (TT_KEYWORD, 'case') and
                       self.peek() != (TT_KEYWORD, 'default') and
                       self.peek() != (TT_KEYWORD, 'end') and
                       not self.at_end()):
                    if self.match(TT_SEMICOLON):
                        continue
                    stmt = self.parse_statement()
                    if stmt:
                        body.append(stmt)
                cases.append({'value': case_val, 'body': body})
            elif self.match(TT_KEYWORD, 'default'):
                self.expect(TT_OP, ':')
                default_body = []
                while (self.peek() != (TT_KEYWORD, 'case') and
                       self.peek() != (TT_KEYWORD, 'end') and
                       not self.at_end()):
                    if self.match(TT_SEMICOLON):
                        continue
                    stmt = self.parse_statement()
                    if stmt:
                        default_body.append(stmt)
            else:
                break
        self.expect(TT_KEYWORD, 'end')
        self.match(TT_SEMICOLON)
        return {'type': 'switch', 'expr': expr, 'cases': cases, 'default': default_body}

    # ---- Array Declaration ----
    def parse_attribute(self):
        # Strategy/study attribute: [Name = Value] or [Name(args)] or [Name].
        # Parsed for completeness; semantically a no-op for the per-bar calc/trace
        # output (behaviour attributes like SameExitFromOneEntryOnce /
        # IntrabarOrderGeneration do not change the dumped column values).
        self.expect(TT_LBRACKET)
        name = self.expect(TT_IDENT)[1]
        value = None
        raw_args = None
        if self.match(TT_OP, '='):
            value = self.parse_expression()
        elif self.peek()[0] == TT_LPAREN:
            # [Name(args)] form — consume the balanced parenthesised arg list,
            # retaining the raw inner token text (e.g. [Data(2)],
            # [IntrabarOrderGeneration(...)]) so the reverse emitter can reproduce
            # the attribute; dropping it would change MC deploy fill behaviour.
            self.advance()  # consume '('
            depth = 1
            inner = []
            while depth > 0 and self.peek()[0] != TT_EOF:
                t = self.advance()
                if t[0] == TT_LPAREN:
                    depth += 1
                    inner.append(t)
                elif t[0] == TT_RPAREN:
                    depth -= 1
                    if depth == 0:
                        break
                    inner.append(t)
                else:
                    inner.append(t)
            raw_args = _reconstruct_tokens(inner)
        self.expect(TT_RBRACKET)
        self.match(TT_SEMICOLON)
        node = {'type': 'attribute', 'name': name, 'value': value}
        if raw_args is not None:
            node['raw_args'] = raw_args
        return node

    def parse_array_decl(self):
        self.advance()  # consume 'arrays'/'array'
        self.match(TT_OP, ':')  # optional colon
        decls = []
        while True:
            # IntraBarPersist modifier may precede an array name too; retain it
            # rather than choking (codegen ignores the flag).
            intrabar = self.match(TT_KEYWORD, 'intrabarpersist') is not None
            name_index = self.pos
            name = self.expect(TT_IDENT)[1]
            ident = self._ident_spelling(name, name_index)
            self.expect(TT_LBRACKET)
            # Dynamic array: empty brackets [] with no size expression
            if self.match(TT_RBRACKET):
                size = None  # dynamic / resizable array
            else:
                size = self.parse_expression()
                self.expect(TT_RBRACKET)
            init = {'type': 'number', 'value': '0'}
            if self.match(TT_LPAREN):
                init = self.parse_expression()
                self.expect(TT_RPAREN)
            decl = {'name': name, 'size': size, 'init': init}
            if intrabar:
                decl['intrabar_persist'] = True
            if ident is not None:
                decl['_ident'] = ident
            decls.append(decl)
            if not self.match(TT_COMMA):
                break
        self.match(TT_SEMICOLON)
        return {'type': 'array_decl', 'decls': decls}

    # ---- Commentary ----
    def parse_commentary(self):
        self.advance()  # 'commentary'
        self.expect(TT_LPAREN)
        msg = self.parse_expression()
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        return {'type': 'commentary', 'msg': msg}

    # ---- Order ----
    def parse_order(self):
        action = self.advance()[1]  # buy/sell/sellshort/buytocover
        label = ''
        quantity = None
        total = False

        # Optional label in parens (string literal or expression)
        if self.match(TT_LPAREN):
            label = self.parse_expression()
            self.expect(TT_RPAREN)

        # Optional quantity: N shares/contracts
        tok = self.peek()
        if tok[0] == TT_NUMBER or (tok[0] == TT_IDENT and tok[1] not in ('next', 'this', 'from')):
            quantity = self.parse_expression()
            # consume 'shares' or 'contracts' if present
            if self.peek()[0] == TT_IDENT and self.peek()[1] in ('shares', 'contracts', 'share', 'contract'):
                self.advance()
            # Optional 'total' modifier (sell total position) — SEMANTIC for
            # named exits, so retain it for the reverse emitter.
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'total':
                self.advance()
                total = True

        # Optional "from entry("label")" reference (mode 4 named exits)
        entry_label = None
        if self.peek()[0] == TT_IDENT and self.peek()[1] == 'from':
            self.advance()  # 'from'
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'entry':
                self.advance()  # 'entry'
                if self.match(TT_LPAREN):
                    entry_label = self.parse_expression()
                    self.expect(TT_RPAREN)

        # "next bar at market" or "next bar at <price> stop/limit"
        # or "this bar on close"
        bar_timing = 'next'
        order_type = 'market'
        price = None

        if self.match(TT_KEYWORD, 'next'):
            # expect "bar at"
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'bar':
                self.advance()
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'at':
                self.advance()
            # market, limit price, stop price
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'market':
                self.advance()
                order_type = 'market'
            else:
                price = self.parse_expression()
                if self.peek()[0] == TT_IDENT and self.peek()[1] in ('stop', 'limit'):
                    order_type = self.advance()[1]
        elif self.peek()[0] == TT_IDENT and self.peek()[1] == 'this':
            self.advance()  # 'this'
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'bar':
                self.advance()  # 'bar'
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'on':
                self.advance()  # 'on'
            if self.peek()[0] == TT_IDENT and self.peek()[1] == 'close':
                self.advance()  # 'close'
            bar_timing = 'this'
            order_type = 'close'

        self.match(TT_SEMICOLON)
        node = {
            'type': 'order',
            'action': action,
            'label': label,
            'quantity': quantity,
            'entry_label': entry_label,
            'bar_timing': bar_timing,
            'order_type': order_type,
            'price': price,
        }
        if total:
            node['total'] = True
        return node

    # ---- Risk Functions ----
    def parse_risk_func(self):
        name = self.advance()[1]
        if name == 'setexitonclose':
            # No args
            if self.match(TT_LPAREN):
                self.expect(TT_RPAREN)
            self.match(TT_SEMICOLON)
            return {'type': 'risk', 'func': name, 'arg': None}
        elif name == 'setpercenttrailing':
            # Two-arg form: SetPercentTrailing(Profit, Pct)
            self.expect(TT_LPAREN)
            arg1 = self.parse_expression()
            self.expect(TT_COMMA)
            arg2 = self.parse_expression()
            self.expect(TT_RPAREN)
            self.match(TT_SEMICOLON)
            return {'type': 'risk', 'func': name, 'args': [arg1, arg2]}
        elif name == 'setdollartrailing':
            self.expect(TT_LPAREN)
            arg = self.parse_expression()
            self.expect(TT_RPAREN)
            self.match(TT_SEMICOLON)
            return {'type': 'risk', 'func': name, 'arg': arg}
        elif name in ('setstopcontract', 'setstopshare', 'setstopposition'):
            # Bare risk functions (no parentheses)
            if self.match(TT_LPAREN):
                self.expect(TT_RPAREN)
            self.match(TT_SEMICOLON)
            return {'type': 'risk', 'func': name, 'arg': None}
        else:
            self.expect(TT_LPAREN)
            arg = self.parse_expression()
            self.expect(TT_RPAREN)
            self.match(TT_SEMICOLON)
            return {'type': 'risk', 'func': name, 'arg': arg}

    # ---- Plot ----
    def parse_plot(self):
        name = self.advance()[1]  # plot1, plot2, etc.
        self.expect(TT_LPAREN)
        value = self.parse_expression()
        label = None
        extra_args = []
        if self.match(TT_COMMA):
            label_expr = self.parse_expression()
            # Extract string value if it's a literal, otherwise store as expression
            if label_expr.get('type') == 'string':
                label = label_expr['value']
            else:
                label = label_expr  # store as AST node for codegen
            # Retain optional extra args (color/style/width) for reverse emit;
            # codegen ignores them so forward output is unchanged.
            while self.match(TT_COMMA):
                extra_args.append(self.parse_expression())
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        node = {'type': 'plot', 'name': name, 'value': value, 'label': label}
        if extra_args:
            node['extra_args'] = extra_args
        return node

    # ---- Alert ----
    def parse_alert(self):
        self.advance()  # 'alert'
        self.expect(TT_LPAREN)
        msg = self.parse_expression()
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        return {'type': 'alert', 'msg': msg}

    # ---- Print ----
    def parse_print_stmt(self):
        self.advance()  # 'print'
        self.expect(TT_LPAREN)
        args = [self.parse_expression()]
        while self.match(TT_COMMA):
            args.append(self.parse_expression())
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        return {'type': 'print', 'args': args}

    # ---- Assignment or Expression Statement ----
    def parse_assignment_or_expr(self):
        # Lookahead: if we see IDENT = or IDENT := then it's assignment
        # We need to handle the case where IDENT could also be IDENT[...] =
        save_pos = self.pos

        # Try to parse as assignment first
        if self.peek()[0] == TT_IDENT:
            target = self._parse_ident_or_call()

            # Check for := assignment
            if self.match(TT_OP, ':='):
                rhs = self.parse_expression()
                self.match(TT_SEMICOLON)
                return {'type': 'assign', 'target': target, 'value': rhs}

            # Check for = assignment (only if target is ident or bar_ref, not a call)
            if self.peek() == (TT_OP, '=') and target.get('type') in ('ident', 'bar_ref'):
                self.advance()  # consume =
                rhs = self.parse_expression()
                self.match(TT_SEMICOLON)
                return {'type': 'assign', 'target': target, 'value': rhs}

            # Not an assignment, backtrack and parse as expression
            self.pos = save_pos

        expr = self.parse_expression()
        self.match(TT_SEMICOLON)
        return {'type': 'expr_stmt', 'expr': expr}

    # ---- Expression: or ----
    def parse_expression(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.peek() == (TT_KEYWORD, 'or'):
            self.advance()
            right = self.parse_and()
            left = {'type': 'binop', 'op': 'or', 'left': left, 'right': right}
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.peek() == (TT_KEYWORD, 'and'):
            self.advance()
            right = self.parse_not()
            left = {'type': 'binop', 'op': 'and', 'left': left, 'right': right}
        return left

    def parse_not(self):
        if self.peek() == (TT_KEYWORD, 'not'):
            self.advance()
            operand = self.parse_not()
            return {'type': 'unaryop', 'op': 'not', 'operand': operand}
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_addition()
        while True:
            tok = self.peek()
            if tok[0] == TT_OP and tok[1] in ('=', '<', '>', '>=', '<=', '<>'):
                op = self.advance()[1]
                right = self.parse_addition()
                left = {'type': 'compare', 'op': op, 'left': left, 'right': right}
            elif tok[0] == TT_KEYWORD and tok[1] in ('crossesabove', 'crossesbelow'):
                name = self.advance()[1]
                right = self.parse_addition()
                left = {'type': 'call', 'name': name, 'args': [left, right]}
            else:
                break
        return left

    def parse_addition(self):
        left = self.parse_multiplication()
        while self.peek()[0] == TT_OP and self.peek()[1] in ('+', '-'):
            op = self.advance()[1]
            right = self.parse_multiplication()
            left = {'type': 'binop', 'op': op, 'left': left, 'right': right}
        return left

    def parse_multiplication(self):
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok[0] == TT_OP and tok[1] in ('*', '/'):
                op = self.advance()[1]
                right = self.parse_unary()
                left = {'type': 'binop', 'op': op, 'left': left, 'right': right}
            elif tok[0] == TT_KEYWORD and tok[1] == 'mod':
                self.advance()
                right = self.parse_unary()
                left = {'type': 'binop', 'op': '%', 'left': left, 'right': right}
            else:
                break
        return left

    def parse_unary(self):
        if self.peek()[0] == TT_OP and self.peek()[1] == '-':
            self.advance()
            operand = self.parse_unary()
            return {'type': 'unaryop', 'op': '-', 'operand': operand}
        if self.peek()[0] == TT_OP and self.peek()[1] == '+':
            self.advance()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        tt, val = tok

        # Number
        if tt == TT_NUMBER:
            self.advance()
            return {'type': 'number', 'value': val}

        # String
        if tt == TT_STRING:
            self.advance()
            return {'type': 'string', 'value': val}

        # Boolean keywords
        if tt == TT_KEYWORD and val == 'true':
            self.advance()
            return {'type': 'boolean', 'value': True}
        if tt == TT_KEYWORD and val == 'false':
            self.advance()
            return {'type': 'boolean', 'value': False}

        # MarketPosition
        if tt == TT_IDENT and val == 'marketposition':
            self.advance()
            node = {'type': 'ident', 'name': '_market_position'}
            if self.peek()[0] == TT_LBRACKET:
                node = self._parse_bar_ref(node)
            return node

        # Position info keywords
        _POSITION_KEYWORDS = {
            'entryprice': 'entry_price',
            'exitprice': 'exit_price',
            'positionprofit': 'position_profit',
            'barssinceentry': 'bars_since_entry',
            'barssinceexit': 'bars_since_exit',
            # Order-related keywords
            'grossprofit': 'gross_profit',
            'grossloss': 'gross_loss',
            'netprofit': 'net_profit',
            'maxpositionprofit': 'max_position_profit',
            'maxpositionloss': 'max_position_loss',
            'openpositionprofit': 'open_position_profit',
            'contractprofit': 'contract_profit',
            'entrydate': 'entry_date',
            'entrytime': 'entry_time',
            'exitdate': 'exit_date',
            'exittime': 'exit_time',
            # MC extensions
            'lastbaronchartex': 'lastbaronchartex',
            # Output
            'alertenabled': 'alert_enabled',
            'checkalert': 'check_alert',
            # Additional order keywords
            'maxcontracts': 'max_contracts',
            'currentcontracts': 'current_contracts',
            'maxentries': 'max_entries',
            'maxcontractsheld': 'maxcontractsheld',
            'maxiddrawdown': 'maxiddrawdown',
            # MC checked variants
            'marketposition_checked': '_market_position',
            'positionprofit_checked': 'position_profit',
            'barssinceentry_checked': 'bars_since_entry',
            'barssinceexit_checked': 'bars_since_exit',
            'entryprice_checked': 'entry_price',
            'exitprice_checked': 'exit_price',
            'entrydate_checked': 'entry_date',
            'entrytime_checked': 'entry_time',
            'exitdate_checked': 'exit_date',
            'exittime_checked': 'exit_time',
            'maxpositionprofit_checked': 'max_position_profit',
            'maxpositionloss_checked': 'max_position_loss',
            'contractprofit_checked': 'contract_profit',
            # ---- Auto-generated order keywords ----
            'avgbarseventrade': 'avgbarseventrade',
            'avgbarslostrade': 'avgbarslostrade',
            'avgbarswintrade': 'avgbarswintrade',
            'avgentryprice': 'avgentryprice',
            'avgentryprice_at_broker': 'avgentryprice_at_broker',
            'currententries': 'currententries',
            'currentshares': 'currentshares',
            'entrydatetime': 'entrydatetime',
            'entrydatetime_checked': 'entrydatetime_checked',
            'exitdatetime': 'exitdatetime',
            'exitdatetime_checked': 'exitdatetime_checked',
            'i_avgentryprice': 'i_avgentryprice',
            'i_avgentryprice_at_broker': 'i_avgentryprice_at_broker',
            'i_marketposition': 'i_marketposition',
            'i_marketposition_at_broker': 'i_marketposition_at_broker',
            'largestlostrade': 'largestlostrade',
            'largestwintrade': 'largestwintrade',
            'marketposition_at_broker': 'marketposition_at_broker',
            'maxconseclosers': 'maxconseclosers',
            'maxconsecwinners': 'maxconsecwinners',
            'maxcontractprofit': 'maxcontractprofit',
            'maxpositionsago': 'maxpositionsago',
            'numeventrades': 'numeventrades',
            'numlostrades': 'numlostrades',
            'numwintrades': 'numwintrades',
            'openentrycomission': 'openentrycomission',
            'openentrycontracts': 'openentrycontracts',
            'openentrydate': 'openentrydate',
            'openentrymaxprofit': 'openentrymaxprofit',
            'openentrymaxprofitpercontract': 'openentrymaxprofitpercontract',
            'openentryminprofit': 'openentryminprofit',
            'openentryminprofitpercontract': 'openentryminprofitpercontract',
            'openentryprice': 'openentryprice',
            'openentryprofit': 'openentryprofit',
            'openentryprofitpercontract': 'openentryprofitpercontract',
            'openentrytime': 'openentrytime',
            'percentprofit': 'percentprofit',
            'portfolio_grossloss': 'portfolio_grossloss',
            'portfolio_grossprofit': 'portfolio_grossprofit',
            'portfolio_netprofit': 'portfolio_netprofit',
            'portfolio_numlosstrades': 'portfolio_numlosstrades',
            'portfolio_numwintrades': 'portfolio_numwintrades',
            'portfolio_openpositionprofit': 'portfolio_openpositionprofit',
            'portfolio_percentprofit': 'portfolio_percentprofit',
            'portfolio_totaltrades': 'portfolio_totaltrades',
            'postradecommission': 'postradecommission',
            'postradecount': 'postradecount',
            'postradeentrybar': 'postradeentrybar',
            'postradeentrycategory': 'postradeentrycategory',
            'postradeentrydatetime': 'postradeentrydatetime',
            'postradeentryname': 'postradeentryname',
            'postradeentryprice': 'postradeentryprice',
            'postradeexitbar': 'postradeexitbar',
            'postradeexitcategory': 'postradeexitcategory',
            'postradeexitdatetime': 'postradeexitdatetime',
            'postradeexitname': 'postradeexitname',
            'postradeexitprice': 'postradeexitprice',
            'postradeislong': 'postradeislong',
            'postradeisopen': 'postradeisopen',
            'postradeprofit': 'postradeprofit',
            'postradesize': 'postradesize',
            'totalbarseventrades': 'totalbarseventrades',
            'totalbarslostrades': 'totalbarslostrades',
            'totalbarswintrades': 'totalbarswintrades',
            'totaltrades': 'totaltrades',
            'tradedate': 'tradedate',
            'tradetime': 'tradetime',
            'tradevolume': 'tradevolume',
        }
        if tt == TT_IDENT and val in _POSITION_KEYWORDS:
            self.advance()
            node = {'type': 'position_kw', 'kwarg': _POSITION_KEYWORDS[val]}
            # Function call form: EntryDate(Value1) -> treat as index
            if self.peek()[0] == TT_LPAREN:
                self.advance()
                idx = self.parse_expression()
                # 2-arg form: PosTradeProfit(PosAgo, TradeNumber)
                if self.peek()[0] == TT_COMMA:
                    self.advance()
                    arg2 = self.parse_expression()
                    self.expect(TT_RPAREN)
                    node = {'type': 'position_kw_call', 'kwarg': node['kwarg'],
                            'args': [idx, arg2]}
                else:
                    self.expect(TT_RPAREN)
                    node = {'type': 'bar_ref', 'series': node, 'index': idx}
            # Historical index: position_kw[n] (e.g. EntryTime[1])
            elif self.peek()[0] == TT_LBRACKET:
                node = self._parse_bar_ref(node)
            return node

        # Extra data series (not function params — accessed via kwargs)
        # NOTE: some of these are also function names (DayOfWeek(Date) etc.)
        # If followed by '(', treat as function call instead.
        _DATA_SERIES_KEYWORDS = {
            'ticks': 'ticks', 'upticks': 'upticks', 'downticks': 'downticks',
            'openint': 'openint', 'barnumber': 'barnumber', 'currentbar': 'currentbar',
            'lastbaronchart': 'lastbaronchart', 'barstatus': 'barstatus',
            'bartype': 'bartype', 'barinterval': 'barinterval',
            'sessionnumber': 'sessionnumber', 'dayofweek': 'dayofweek',
            'dayofmonth': 'dayofmonth', 'month': 'month', 'year': 'year',
            'currentdate': 'current_date', 'currenttime': 'current_time',
            'datetime': 'datetime', 'sess1starttime': 'sess1starttime',
            'sessionstarttime': 'sessionstarttime', 'sessionendtime': 'sessionendtime',
            'currentsession': 'currentsession',
            'dailyopen': 'dailyopen', 'dailyhigh': 'dailyhigh', 'dailylow': 'dailylow',
            'dailyclose': 'dailyclose', 'prevclose': 'prevclose',
            'pointvalue': 'pointvalue', 'bigpointvalue': 'bigpointvalue',
            'minmove': 'minmove', 'pricescale': 'pricescale',
        }
        if tt == TT_IDENT and val in _DATA_SERIES_KEYWORDS:
            # If followed by '(', it's a function call (e.g. DayOfWeek(Date))
            if self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1][0] == TT_LPAREN:
                return self._parse_ident_or_call()
            self.advance()
            return {'type': 'position_kw', 'kwarg': _DATA_SERIES_KEYWORDS[val]}

        # Color constants — fold to a number (codegen reads 'value'), but retain
        # the original symbol name in '_symbol' for faithful reverse emission.
        if tt == TT_IDENT and val in COLOR_CONSTANTS:
            self.advance()
            return {'type': 'number', 'value': str(COLOR_CONSTANTS[val]), '_symbol': val}

        # Style constants
        if tt == TT_IDENT and val in STYLE_CONSTANTS:
            self.advance()
            return {'type': 'number', 'value': str(STYLE_CONSTANTS[val]), '_symbol': val}

        # Period data functions (OpenD, HighD, etc.)
        if tt == TT_IDENT and val in PERIOD_FUNCS:
            return self._parse_ident_or_call()

        # MC extension identifiers (no-arg keywords)
        _MC_KEYWORDS = {
            'time_s': 'time_s',
            'bartype_ex': 'bartype_ex',
        }
        if tt == TT_IDENT and val in _MC_KEYWORDS:
            self.advance()
            return {'type': 'mc_kw', 'kwarg': _MC_KEYWORDS[val]}

        # CrossesAbove / CrossesBelow as keywords
        if tt == TT_KEYWORD and val in ('crossesabove', 'crossesbelow'):
            return self._parse_func_call()

        # Mod as a function form: Mod(a, b) -> a % b (same node the infix `mod` produces).
        # NOTE: matches Python % for non-negative operands (EL usage here); negative-operand
        # EL Mod semantics are a runtime concern for U5.
        if tt == TT_KEYWORD and val == 'mod':
            self.advance()
            self.expect(TT_LPAREN)
            a = self.parse_expression()
            self.expect(TT_COMMA)
            b = self.parse_expression()
            self.expect(TT_RPAREN)
            return {'type': 'binop', 'op': '%', 'left': a, 'right': b}

        # Parenthesized expression
        if tt == TT_LPAREN:
            self.advance()
            expr = self.parse_expression()
            self.expect(TT_RPAREN)
            return expr

        # Multi-data accessor: "Close of Data2", "High of Data(2)", etc.
        _DATA_OF_SERIES = {'close', 'high', 'low', 'open', 'volume', 'date', 'time',
                           'ticks', 'upticks', 'downticks', 'openint'}
        if tt == TT_IDENT and val in _DATA_OF_SERIES:
            if (self.pos + 2 < len(self.tokens) and
                self.tokens[self.pos + 1][0] == TT_IDENT and
                self.tokens[self.pos + 1][1] == 'of'):
                data_tok = self.tokens[self.pos + 2]
                # Form: Close of Data2
                if (data_tok[0] == TT_IDENT and
                    data_tok[1].startswith('data') and
                    data_tok[1][4:].isdigit()):
                    self.advance()  # series ident
                    self.advance()  # 'of'
                    self.advance()  # DataN
                    return {'type': 'data_of', 'series': val,
                            'data_num': int(data_tok[1][4:])}
                # Form: Close of Data(2)
                if (data_tok[0] == TT_IDENT and data_tok[1] == 'data' and
                    self.pos + 5 < len(self.tokens) and
                    self.tokens[self.pos + 3][0] == TT_LPAREN and
                    self.tokens[self.pos + 4][0] == TT_NUMBER and
                    self.tokens[self.pos + 5][0] == TT_RPAREN):
                    self.advance()  # series ident
                    self.advance()  # 'of'
                    self.advance()  # 'Data'
                    self.advance()  # '('
                    num_tok = self.advance()  # number
                    self.advance()  # ')'
                    return {'type': 'data_of', 'series': val,
                            'data_num': int(num_tok[1])}

        # Identifier — could be func call, bar ref, or plain ident
        if tt == TT_IDENT or (tt == TT_KEYWORD and val in BUILTIN_FUNC_MAP):
            return self._parse_ident_or_call()

        self._err(
            f"unexpected {self._describe(tok)} where an expression was expected",
            hint="check for a missing operator, operand, or a typo'd keyword")

    def _parse_ident_or_call(self):
        name_index = self.pos
        tok = self.advance()
        name = tok[1]
        line, col = self._pos_of(name_index)
        ident = self._ident_spelling(name, name_index)

        # Function call
        if self.peek()[0] == TT_LPAREN:
            self.advance()  # consume (
            args = []
            if self.peek()[0] != TT_RPAREN:
                args.append(self.parse_expression())
                while self.match(TT_COMMA):
                    args.append(self.parse_expression())
            self.expect(TT_RPAREN)
            node = {'type': 'call', 'name': name, 'args': args,
                    '_line': line, '_col': col}
            if ident is not None:
                node['_ident'] = ident
            # Historical index: func()[n]
            if self.peek()[0] == TT_LBRACKET:
                node = self._parse_bar_ref(node)
            return node

        node = {'type': 'ident', 'name': name, '_line': line, '_col': col}
        if ident is not None:
            node['_ident'] = ident

        # Bar reference: ident[expr]
        if self.peek()[0] == TT_LBRACKET:
            node = self._parse_bar_ref(node)

        return node

    def _parse_func_call(self):
        """Parse a keyword-based function call like crossesabove(a, b)."""
        name = self.advance()[1]
        self.expect(TT_LPAREN)
        args = []
        if self.peek()[0] != TT_RPAREN:
            args.append(self.parse_expression())
            while self.match(TT_COMMA):
                args.append(self.parse_expression())
        self.expect(TT_RPAREN)
        return {'type': 'call', 'name': name, 'args': args}

    def _parse_bar_ref(self, node):
        self.advance()  # consume [
        index = self.parse_expression()
        self.expect(TT_RBRACKET)
        return {'type': 'bar_ref', 'series': node, 'index': index}


# ---------------------------------------------------------------------------
# Semantic validation (post-parse)
# ---------------------------------------------------------------------------

# Bare data-series / reserved identifiers that codegen resolves implicitly
# (CodeGen._SERIES_PARAMS and _BARE_COMPUTED) and which therefore must NOT be
# reported as undefined by the semantic pass.
_SERIES_PARAMS = {'open', 'high', 'low', 'close', 'volume', 'date', 'time'}
_BARE_COMPUTED = {
    'truerange', 'medianprice', 'range', 'avgprice', 'truehigh', 'truelow',
    'typicalprice', 'weightedclose', 'currentdate', 'currenttime',
    'ticks', 'upticks', 'downticks', 'openint',
}

# PL auto-declared scratch variables Value0..Value99 / Condition0..Condition99,
# plus assorted reserved words that survive parsing as bare idents.
_SCRATCH_VARS = ({f'value{i}' for i in range(100)} |
                 {f'condition{i}' for i in range(100)})
_RESERVED_IDENTS = _SCRATCH_VARS | {
    # Runtime internals emitted by the generated wrapper's prologue
    # (codegen header) — these can resurface as bare idents in round-tripped EL
    # (e.g. the reverse emitter renders a Commentary statement as
    # `_commentary = <text>`). They are legitimate known names, NOT unimplemented
    # keywords, so the FL5 RC3 lvalue guard must never flag a write to one.
    '_market_position', 'marketposition', '_commentary',
    'all', 'newline', 'pi',
    'lastbaronchart_s', 'lastbaronchart',
    # order-quantity / timing words that may surface as bare idents
    'next', 'this', 'bar', 'at', 'market', 'on', 'shares', 'contracts',
    'share', 'contract', 'total', 'stop', 'limit',
}


def _collect_declared(ast):
    """Names introduced by Input/Variable/Array declarations and for-loop vars."""
    declared = set()

    def walk(n):
        if isinstance(n, dict):
            t = n.get('type')
            if t in ('var_decl', 'input_decl', 'array_decl'):
                for d in n.get('decls', []):
                    declared.add(d['name'])
            elif t == 'for':
                declared.add(n['var'])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(ast)
    return declared


def _is_known_name(name, declared):
    """True if `name` is a legitimate identifier (declared or a known builtin)."""
    if name in declared:
        return True
    if name in BUILTIN_FUNC_MAP or name in BUILTIN_ARITY:
        return True
    if name in _SERIES_PARAMS or name in DATA_SERIES or name in _BARE_COMPUTED:
        return True
    if name in _RESERVED_IDENTS or name in ORDER_ACTIONS or name in RISK_FUNCS:
        return True
    if name in PLOT_NAMES or name in PERIOD_FUNCS:
        return True
    if name in COLOR_CONSTANTS or name in STYLE_CONSTANTS:
        return True
    return False


def semantic_check(ast, source='', strict_undefined=False):
    """Post-parse semantic validation.

    Catches, with precise line:col locations:
      (a) wrong arity for KNOWN builtins (BUILTIN_ARITY), and
      (b) undefined identifiers — used but not declared and not a known builtin.

    Arity errors are always raised (high confidence). Undefined-identifier
    findings are returned as a list of warning strings by default; pass
    strict_undefined=True to raise on the first one instead. This keeps valid
    code untouched while still surfacing typos for the LLM to self-correct.
    """
    declared = _collect_declared(ast)
    warnings = []

    def err(message, node, hint=''):
        line = node.get('_line', 0)
        col = node.get('_col', 0)
        raise PLSyntaxError(message, line, col, _source_line(source, line), hint)

    def walk(n):
        if isinstance(n, dict):
            t = n.get('type')
            if t == 'call':
                name = n.get('name', '')
                arity = BUILTIN_ARITY.get(name)
                if arity is not None:
                    nargs = len(n.get('args', []))
                    if nargs not in arity:
                        want = ' or '.join(str(a) for a in sorted(arity))
                        err(f"'{name}' expects {want} argument(s) but got {nargs}",
                            n,
                            hint=f"check the call's arguments — {name} takes {want}")
            elif t == 'ident':
                name = n.get('name', '')
                if not _is_known_name(name, declared):
                    line = n.get('_line', 0)
                    col = n.get('_col', 0)
                    msg = (f"undefined identifier '{name}' — not declared as an "
                           f"Input/Variable/Array and not a known builtin")
                    if strict_undefined:
                        err(msg, n, hint="declare it (Variables: %s(0);) or fix the spelling" % name)
                    else:
                        warnings.append(
                            f"line {line}:{col}: {msg}")
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(ast)
    return warnings


def parse(tokens, positions=None, source=''):
    """Parse token list into AST, then run the semantic validation pass.

    `positions`/`source` enable EL-aware (line:col + caret) error messages and
    the post-parse semantic checks. Arity errors raise; undefined-identifier
    findings are collected and attached to the AST under '_warnings' (they do
    not block valid code from transpiling).
    """
    p = Parser(tokens, positions=positions, source=source)
    ast = p.parse_program()
    # Attach the verbatim comment side-channel (Rcmt). Derived purely from `source`
    # (comments are stripped from the token stream, so this is additive and never
    # affects behaviour). Empty when no source text was supplied.
    ast['_comments'] = collect_comments(source) if source else []
    # Leading blank lines at the very top of the source (Rcyc-preserved). Counted
    # verbatim from `source` here — the ONLY place EL line-0 whitespace is known —
    # so the emitter can reproduce it without inferring from `_line` (which is
    # unreliable across the py_front two-way path). py_front never sets this key.
    if source:
        n = 0
        for _line in source.split("\n"):
            if _line.strip() == "":
                n += 1
            else:
                if n:
                    ast['_leading_blanks'] = n
                break
    warnings = semantic_check(ast, source=source)
    if warnings:
        ast['_warnings'] = warnings
    return ast
