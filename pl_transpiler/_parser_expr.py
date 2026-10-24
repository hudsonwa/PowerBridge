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


"""Expression-level recursive-descent methods of `parser.Parser` (a mixin)."""

from typing import TYPE_CHECKING, Any, NoReturn, Optional

from ._types import Node, Token
from .lexer import (
    TT_COMMA,
    TT_IDENT,
    TT_KEYWORD,
    TT_LBRACKET,
    TT_LPAREN,
    TT_NUMBER,
    TT_OP,
    TT_RBRACKET,
    TT_RPAREN,
    TT_STRING,
)
from .parser_tables import (
    _PRICE_ABBREVIATIONS,
    _SYMBOL_INFO_KEYWORDS,
    BUILTIN_FUNC_MAP,
    COLOR_CONSTANTS,
    PERIOD_FUNCS,
    STYLE_CONSTANTS,
)

# Position/strategy-performance keywords -> `position_kw` kwarg names.
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
    'maxshares': 'maxshares',
    'maxsharesheld': 'maxcontractsheld',
    'openentriescount': 'openentriescount',
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

# Extra data series (not function params — accessed via kwargs).
# NOTE: some of these are also function names (DayOfWeek(Date) etc.);
# if followed by '(', they parse as a function call instead.
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
    'symbol': 'symbol', 'symbolname': 'symbolname',
    'description': 'description', 'category': 'category',
    'symbolcurrencycode': 'symbolcurrencycode',
    'expirationdate': 'expirationdate',
}

# MC extension identifiers (no-arg keywords).
_MC_KEYWORDS = {
    'time_s': 'time_s',
    'bartype_ex': 'bartype_ex',
}

# Series that accept the multi-data form `Close of Data2` / `High of Data(2)`.
_DATA_OF_SERIES = {'close', 'high', 'low', 'open', 'volume', 'date', 'time',
                   'ticks', 'upticks', 'downticks', 'openint'}


class _ExpressionsMixin:
    if TYPE_CHECKING:
        # Host interface supplied by `parser.Parser` (type-checking only).
        tokens: list[Token]
        positions: list[tuple[int, int]]
        source: str
        pos: int
        _user_names: set[str]
        _declared_names: set[str]

        def peek(self) -> Token: ...

        def advance(self) -> Token: ...

        def _pos_of(self, tok_index: int) -> tuple[int, int]: ...

        def _err(self, message: str, hint: str = '', tok_index: Optional[int] = None) -> NoReturn: ...

        @staticmethod
        def _describe(tok: Token) -> str: ...

        def expect(self, tt: str, val: Any = None) -> Token: ...

        def match(self, tt: str, val: Any = None) -> Optional[Token]: ...

        def _ident_spelling(self, name: str, tok_index: int) -> Optional[str]: ...

    # ---- Expression: or ----
    def parse_expression(self) -> Node:
        return self.parse_or()

    def parse_or(self) -> Node:
        left = self.parse_and()
        while self.peek() == (TT_KEYWORD, 'or'):
            self.advance()
            right = self.parse_and()
            left = {'type': 'binop', 'op': 'or', 'left': left, 'right': right}
        return left

    def parse_and(self) -> Node:
        left = self.parse_not()
        while self.peek() == (TT_KEYWORD, 'and'):
            self.advance()
            right = self.parse_not()
            left = {'type': 'binop', 'op': 'and', 'left': left, 'right': right}
        return left

    def parse_not(self) -> Node:
        if self.peek() == (TT_KEYWORD, 'not'):
            self.advance()
            operand = self.parse_not()
            return {'type': 'unaryop', 'op': 'not', 'operand': operand}
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
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

    def parse_addition(self) -> Node:
        left = self.parse_multiplication()
        while self.peek()[0] == TT_OP and self.peek()[1] in ('+', '-'):
            op = self.advance()[1]
            right = self.parse_multiplication()
            left = {'type': 'binop', 'op': op, 'left': left, 'right': right}
        return left

    def parse_multiplication(self) -> Node:
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

    def parse_unary(self) -> Node:
        if self.peek()[0] == TT_OP and self.peek()[1] == '-':
            self.advance()
            operand = self.parse_unary()
            return {'type': 'unaryop', 'op': '-', 'operand': operand}
        if self.peek()[0] == TT_OP and self.peek()[1] == '+':
            self.advance()
            return self.parse_unary()
        node = self.parse_primary()
        if self.peek()[0] == TT_IDENT and self.peek()[1] in ('point', 'points'):
            # `N Point(s)`: N minimum price moves, N * (MinMove / PriceScale).
            self.advance()
            tick = {'type': 'binop', 'op': '/',
                    'left': {'type': 'position_kw', 'kwarg': 'minmove'},
                    'right': {'type': 'position_kw', 'kwarg': 'pricescale'}}
            node = {'type': 'binop', 'op': '*', 'left': node, 'right': tick}
        return node

    def parse_primary(self) -> Node:
        """Parse one primary expression, dispatching on the next token's kind."""
        tok = self.peek()
        tt, val = self._resolve_price_abbreviation(tok)
        if tt in (TT_NUMBER, TT_STRING):
            self.advance()
            return {'type': 'number' if tt == TT_NUMBER else 'string', 'value': val}
        if tt == TT_IDENT:
            return self._parse_ident_primary(val)
        if tt == TT_KEYWORD:
            node = self._parse_keyword_primary(val)
            if node is not None:
                return node
        elif tt == TT_LPAREN:
            # Parenthesized expression
            self.advance()
            expr = self.parse_expression()
            self.expect(TT_RPAREN)
            return expr
        self._err(
            f"unexpected {self._describe(tok)} where an expression was expected",
            hint="check for a missing operator, operand, or a typo'd keyword")

    def _next_is_lparen(self) -> bool:
        """True when the token after the current one is `(`."""
        return self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1][0] == TT_LPAREN

    def _resolve_price_abbreviation(self, tok: Token) -> Token:
        """Map O/H/L/C/V/I to Open/High/Low/Close/Volume/OpenInt.

        Documented behaviour: a declared input/variable of the same name
        shadows the abbreviation, and `I(...)` stays a function call. Only the
        returned value changes (the long-form node); the token itself keeps its
        original spelling.
        """
        tt, val = tok
        if (tt == TT_IDENT and val in _PRICE_ABBREVIATIONS
                and val not in self._declared_names
                and not self._next_is_lparen()):
            return TT_IDENT, _PRICE_ABBREVIATIONS[val]
        return tok

    def _parse_keyword_primary(self, val: Any) -> Optional[Node]:
        """Keyword-led primaries; None when the keyword cannot start one."""
        # Boolean keywords
        if val in ('true', 'false'):
            self.advance()
            return {'type': 'boolean', 'value': val == 'true'}

        # CrossesAbove / CrossesBelow as keywords
        if val in ('crossesabove', 'crossesbelow'):
            return self._parse_func_call()

        # Mod as a function form: Mod(a, b) -> a % b (same node the infix `mod` produces).
        # NOTE: matches Python % for non-negative operands (EL usage here); negative-operand
        # EL Mod semantics are a runtime concern.
        if val == 'mod':
            self.advance()
            self.expect(TT_LPAREN)
            a = self.parse_expression()
            self.expect(TT_COMMA)
            b = self.parse_expression()
            self.expect(TT_RPAREN)
            return {'type': 'binop', 'op': '%', 'left': a, 'right': b}

        if val in BUILTIN_FUNC_MAP:
            return self._parse_ident_or_call(val)
        return None

    def _parse_ident_primary(self, val: Any) -> Node:
        """Identifier-led primaries, tried in precedence order.

        Reserved words (MarketPosition, position/performance keywords, data
        series, symbol info, colour/style constants, period functions, MC
        keywords, `X of DataN`) come first; anything else is a function call,
        bar reference or plain identifier.
        """
        if val == 'marketposition':
            self.advance()
            node: Node = {'type': 'ident', 'name': '_market_position'}
            if self.peek()[0] == TT_LBRACKET:
                node = self._parse_bar_ref(node)
            return node

        # A user name shadows a position keyword unless it is called like a function.
        if val in _POSITION_KEYWORDS and not (
                val in self._user_names and not self._next_is_lparen()):
            return self._parse_position_kw(val)

        if val in _SYMBOL_INFO_KEYWORDS:
            return self._parse_symbol_info_kw(val)
        if val in _DATA_SERIES_KEYWORDS:
            # If followed by '(', it's a function call (e.g. DayOfWeek(Date))
            if self._next_is_lparen():
                return self._parse_ident_or_call()
            self.advance()
            return {'type': 'position_kw', 'kwarg': _DATA_SERIES_KEYWORDS[val]}

        # Color constants — fold to a number (codegen reads 'value'), but retain
        # the original symbol name in '_symbol' for faithful reverse emission.
        if val in COLOR_CONSTANTS:
            self.advance()
            return {'type': 'number', 'value': str(COLOR_CONSTANTS[val]), '_symbol': val}

        # Style constants
        if val in STYLE_CONSTANTS:
            self.advance()
            return {'type': 'number', 'value': str(STYLE_CONSTANTS[val]), '_symbol': val}

        # Period data functions (OpenD, HighD, etc.)
        if val in PERIOD_FUNCS:
            return self._parse_ident_or_call()

        if val in _MC_KEYWORDS and val not in self._user_names:
            self.advance()
            return {'type': 'mc_kw', 'kwarg': _MC_KEYWORDS[val]}

        if val in _DATA_OF_SERIES:
            data_of = self._try_parse_data_of(val)
            if data_of is not None:
                return data_of

        # Identifier — could be func call, bar ref, or plain ident
        return self._parse_ident_or_call(val)

    def _parse_position_kw(self, val: Any) -> Node:
        """A position keyword, plain or in its call / historical-index form."""
        self.advance()
        node: Node = {'type': 'position_kw', 'kwarg': _POSITION_KEYWORDS[val]}
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

    def _parse_symbol_info_kw(self, val: Any) -> Node:
        """A symbol-info property with its optional data-stream argument.

        e.g. SymbolName(1). Runs have one instrument, so the argument is
        evaluated for its references and otherwise ignored.
        """
        self.advance()
        if self.peek()[0] == TT_LPAREN:
            self.advance()
            if self.peek()[0] != TT_RPAREN:
                self.parse_expression()
            self.expect(TT_RPAREN)
        return {'type': 'position_kw', 'kwarg': _DATA_SERIES_KEYWORDS[val]}

    def _try_parse_data_of(self, val: Any) -> Optional[Node]:
        """Multi-data accessor `Close of Data2` / `High of Data(2)`, else None."""
        if not (self.pos + 2 < len(self.tokens) and
                self.tokens[self.pos + 1][0] == TT_IDENT and
                self.tokens[self.pos + 1][1] == 'of'):
            return None
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
        return None

    def _parse_ident_or_call(self, name: Optional[str] = None) -> Node:
        name_index = self.pos
        tok = self.advance()
        if name is None:
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

    def _parse_func_call(self) -> Node:
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

    def _parse_bar_ref(self, node: Node) -> Node:
        self.advance()  # consume [
        index = self.parse_expression()
        self.expect(TT_RBRACKET)
        return {'type': 'bar_ref', 'series': node, 'index': index}
