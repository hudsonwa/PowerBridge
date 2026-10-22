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

"""PowerLanguage runtime: platform builtins with no backtest meaning (account,
position, portfolio, DOM, quotes, environment, strategy settings).

Most are trivial stubs that pl_runtime turns into loud stubs.
Re-exported by pl_runtime; see that module."""


def pl_dom_askprice(*args, **kwargs):
    return 0

def pl_dom_askscount(*args, **kwargs):
    return 0

def pl_dom_asksize(*args, **kwargs):
    return 0

def pl_dom_bidprice(*args, **kwargs):
    return 0

def pl_dom_bidscount(*args, **kwargs):
    return 0

def pl_dom_bidsize(*args, **kwargs):
    return 0

def pl_dom_isconnected(*args, **kwargs):
    return 0

def pl_exchlisted(*args, **kwargs):
    return 0

def pl_execoffset(*args, **kwargs):
    # ExecOffset matches MultiCharts backtests: 0 on every historical bar
    # (checked against a MultiCharts capture). Whitelisted in _rt_core so it
    # is a real builtin, not a loud stub.
    return 0

def pl_expirationdatefromvendor(*args, **kwargs):
    return 0

def pl_fileappend(*args, **kwargs):
    return 0

def pl_filedelete(*args, **kwargs):
    return 0

def pl_getaccount(*args, **kwargs):
    return 0

def pl_getaccountid(*args, **kwargs):
    return 0

def pl_getappinfo(*args, **kwargs):
    return 0

def pl_getcdromdrive(*args, **kwargs):
    return 0

def pl_getcountry(*args, **kwargs):
    return 0

def pl_getcurrency(*args, **kwargs):
    return 0

def pl_getexchangename(*args, **kwargs):
    return 0

def pl_getnumaccounts(*args, **kwargs):
    return 0

def pl_getnumpositions(*args, **kwargs):
    return 0

def pl_getpositionaverageprice(*args, **kwargs):
    return 0

def pl_getpositionopenpl(*args, **kwargs):
    return 0

def pl_getpositionquantity(*args, **kwargs):
    return 0

def pl_getpositionsymbol(*args, **kwargs):
    return 0

def pl_getpositiontotalcost(*args, **kwargs):
    return 0

def pl_getrtaccountequity(*args, **kwargs):
    return 0

def pl_getrtaccountnetworth(*args, **kwargs):
    return 0

def pl_getrtsymbolname(*args, **kwargs):
    return 0

def pl_getrtunrealizedpl(*args, **kwargs):
    return 0

def pl_getstrategyname(*args, **kwargs):
    return 0

def pl_getsymbolname(*args, **kwargs):
    return 0

def pl_getuserid(*args, **kwargs):
    return 0

def pl_getusername(*args, **kwargs):
    return 0

def pl_i_closedequity(*args, **kwargs):
    return 0

def pl_i_currentcontracts(*args, **kwargs):
    return 0

def pl_i_currentshares(*args, **kwargs):
    return 0

def pl_i_getplotvalue(*args, **kwargs):
    return 0

def pl_i_openequity(*args, **kwargs):
    return 0

def pl_i_setplotvalue(*args, **kwargs):
    return 0

def pl_initialcapital(*args, **kwargs):
    return 0

def pl_insideask(*args, **kwargs):
    return 0

def pl_insidebid(*args, **kwargs):
    return 0

def pl_intrabarordergeneration(*args, **kwargs):
    return 0

def pl_jpy(*args, **kwargs):
    return 0

def pl_last(*args, **kwargs):
    return 0

def pl_lpbool(*args, **kwargs):
    return 0

def pl_lpbyte(*args, **kwargs):
    return 0

def pl_lpdouble(*args, **kwargs):
    return 0

def pl_lpdword(*args, **kwargs):
    return 0

def pl_lpfloat(*args, **kwargs):
    return 0

def pl_lpint(*args, **kwargs):
    return 0

def pl_lplong(*args, **kwargs):
    return 0

def pl_lpstr(*args, **kwargs):
    return 0

def pl_lpword(*args, **kwargs):
    return 0

def pl_margin(*args, **kwargs):
    return 0

def pl_maxbarsback(*args, **kwargs):
    # MaxBarsBack: the study's "Maximum Bars Back" setting (pdf:6483). Fixed chart/
    # study config for the GTA6 capture = 50 (cf. session-config constants).
    return 50

def pl_maxbarsforward(*args, **kwargs):
    # MaxBarsForward: chart right-margin size in bars (pdf:6496). Fixed chart config
    # for the GTA6 capture = 16.
    return 16

def pl_maxcontractsheld(*args, **kwargs):
    return 0

def pl_maxiddrawdown(*args, **kwargs):
    return 0

def pl_messagelog(*args, **kwargs):
    return 0

def pl_minmove(*args, **kwargs):
    return 0

def pl_mouseclickbarnumber(*args, **kwargs):
    return 0

def pl_mouseclickctrlpressed(*args, **kwargs):
    return 0

def pl_mouseclickdatanumber(*args, **kwargs):
    return 0

def pl_mouseclickdatetime(*args, **kwargs):
    return 0

def pl_mouseclickprice(*args, **kwargs):
    return 0

def pl_mouseclickshiftpressed(*args, **kwargs):
    return 0

def pl_optiontype(*args, **kwargs):
    return 0

def pl_placemarketorder(*args, **kwargs):
    return 0

def pl_playsound(*args, **kwargs):
    return 0

def pl_pmm_get_global_named_num(*args, **kwargs):
    return 0

def pl_pmm_get_global_named_str(*args, **kwargs):
    return 0

def pl_pmm_get_my_named_num(*args, **kwargs):
    return 0

def pl_pmm_get_my_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_global_named_num(*args, **kwargs):
    return 0

def pl_pmm_set_global_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_my_named_num(*args, **kwargs):
    return 0

def pl_pmm_set_my_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_my_status(*args, **kwargs):
    return 0

def pl_pmms_get_strategy_named_num(*args, **kwargs):
    return 0

def pl_pmms_get_strategy_named_str(*args, **kwargs):
    return 0

def pl_pmms_set_strategy_named_num(*args, **kwargs):
    return 0

def pl_pmms_set_strategy_named_str(*args, **kwargs):
    return 0

def pl_pmms_strategies_allow_entries_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_deny_entries_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_get_by_symbol_name(*args, **kwargs):
    return 0

def pl_pmms_strategies_in_long_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_in_short_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_pause_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_resume_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_set_status_for_all(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_long_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_short_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_currentcontracts(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_long_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_short_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_is_paused(*args, **kwargs):
    return 0

def pl_pmms_strategy_pause(*args, **kwargs):
    return 0

def pl_pmms_strategy_resume(*args, **kwargs):
    return 0

def pl_pmms_strategy_set_status(*args, **kwargs):
    return 0

def pl_pmms_strategy_symbol(*args, **kwargs):
    return 0

def pl_pointvalue(*args, **kwargs):
    return 0

def pl_portfolio_currencycode(*args, **kwargs):
    return 0

def pl_portfolio_currententries(*args, **kwargs):
    return 0

def pl_portfolio_getmarginpercontract(*args, **kwargs):
    return 0

def pl_portfolio_investedcapital(*args, **kwargs):
    return 0

def pl_portfolio_maxiddrawdown(*args, **kwargs):
    return 0

def pl_portfolio_maxriskequityperpospercent(*args, **kwargs):
    return 0

def pl_portfolio_strategydrawdown(*args, **kwargs):
    return 0

def pl_portfolioentriespriority(*args, **kwargs):
    return 0

def pl_prevclose(*args, **kwargs):
    return 0

def pl_pricescale(*args, **kwargs):
    return 0

def pl_printer(*args, **kwargs):
    return 0

def pl_processmouseevents(*args, **kwargs):
    return 0

def pl_q_ask(*args, **kwargs):
    return 0

def pl_q_asksize(*args, **kwargs):
    return 0

def pl_q_bid(*args, **kwargs):
    return 0

def pl_q_bidsize(*args, **kwargs):
    return 0

def pl_q_bigpointvalue(*args, **kwargs):
    return 0

def pl_q_date(*args, **kwargs):
    return 0

def pl_q_exchangelisted(*args, **kwargs):
    return 0

def pl_q_last(*args, **kwargs):
    return 0

def pl_q_openinterest(*args, **kwargs):
    return 0

def pl_q_previousclose(*args, **kwargs):
    return 0

def pl_q_time(*args, **kwargs):
    return 0

def pl_q_time_dt(*args, **kwargs):
    return 0

def pl_q_time_s(*args, **kwargs):
    return 0

def pl_q_totalvolume(*args, **kwargs):
    return 0

def pl_raiseruntimeerror(*args, **kwargs):
    return 0

def pl_recalclastbarafter(*args, **kwargs):
    return 0

def pl_recalcpersist(*args, **kwargs):
    return 0

def pl_revsize(*args, **kwargs):
    return 0

def pl_sameexitfromoneentryonce(*args, **kwargs):
    return 0

def pl_setcustomfitnessvalue(*args, **kwargs):
    return 0

def pl_setfpcompareaccuracy(*args, **kwargs):
    return 0

def pl_setmaxbarsback(*args, **kwargs):
    return 0

def pl_setstopcontract(*args, **kwargs):
    return 0

def pl_setstopshare(*args, **kwargs):
    return 0

def pl_slippage(*args, **kwargs):
    return 0

def pl_strike(*args, **kwargs):
    return 0

def pl_symbol_close(*args, **kwargs):
    return 0

def pl_symbol_date(*args, **kwargs):
    return 0

def pl_symbol_downticks(*args, **kwargs):
    return 0

def pl_symbol_high(*args, **kwargs):
    return 0

def pl_symbol_low(*args, **kwargs):
    return 0

def pl_symbol_open(*args, **kwargs):
    return 0

def pl_symbol_openint(*args, **kwargs):
    return 0

def pl_symbol_tickid(*args, **kwargs):
    return 0

def pl_symbol_ticks(*args, **kwargs):
    return 0

def pl_symbol_time(*args, **kwargs):
    return 0

def pl_symbol_time_s(*args, **kwargs):
    return 0

def pl_symbol_upticks(*args, **kwargs):
    return 0

def pl_symbol_volume(*args, **kwargs):
    return 0

def pl_varsize(*args, **kwargs):
    return 0

def pl_varstartaddr(*args, **kwargs):
    return 0
