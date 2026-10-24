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


"""Statement-level recursive-descent methods of `parser.Parser` (a mixin)."""

from typing import TYPE_CHECKING, Any, NoReturn, Optional, Union

from ._types import Node, Token
from .lexer import (
    TT_COMMA,
    TT_EOF,
    TT_IDENT,
    TT_KEYWORD,
    TT_LBRACKET,
    TT_LPAREN,
    TT_NUMBER,
    TT_OP,
    TT_RBRACKET,
    TT_RPAREN,
    TT_SEMICOLON,
)


def _reconstruct_tokens(toks: list[Token]) -> str:
    """Best-effort text reconstruction of a consumed token run, used to retain
    attribute arg lists (`[Name(args)]`) in the AST. EL is case- and
    whitespace-insensitive, so this is faithful enough for the reverse emitter
    to reproduce the attribute."""
    out: list[str] = []
    for i, (tt, val) in enumerate(toks):
        if out and tt not in (TT_COMMA, TT_RPAREN, TT_RBRACKET, TT_LPAREN) \
                and toks[i - 1][0] not in (TT_LPAREN, TT_LBRACKET):
            out.append(' ')
        out.append(str(val))
    return ''.join(out)


class _StatementsMixin:
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

        def _err(self, message: str, hint: str = '', tok_index: Optional[int] = None) -> NoReturn: ...

        @staticmethod
        def _describe(tok: Token) -> str: ...

        def expect(self, tt: str, val: Any = None) -> Token: ...

        def match(self, tt: str, val: Any = None) -> Optional[Token]: ...

        def at_end(self) -> bool: ...

        def _ident_spelling(self, name: str, tok_index: int) -> Optional[str]: ...

        def parse_statement(self) -> Optional[Node]: ...

        def parse_expression(self) -> Node: ...

        def _parse_ident_or_call(self, name: Optional[str] = None) -> Node: ...

    # ---- Variable Declaration ----
    def parse_var_decl(self) -> Node:
        self.advance()  # consume 'variables'/'var'
        self.match(TT_OP, ':')  # optional colon
        decls = []
        while True:
            # IntraBarPersist modifier: retain per-decl (semantic for reverse emit;
            # codegen ignores the flag so forward output is unchanged).
            intrabar = self.match(TT_KEYWORD, 'intrabarpersist') is not None
            name_index = self.pos
            name = self.expect(TT_IDENT)[1]
            self._user_names.add(name)  # declared names shadow reserved keywords
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
    def parse_input_decl(self) -> Node:
        self.advance()  # consume 'inputs'
        self.match(TT_OP, ':')
        decls = []
        while True:
            name_index = self.pos
            name = self.expect(TT_IDENT)[1]
            self._user_names.add(name)  # declared names shadow reserved keywords
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
    def parse_if(self) -> Node:
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

        node: Node = {'type': 'if', 'cond': cond, 'body': body, 'else_body': else_body}
        if then_block:
            node['then_block'] = True
        if else_block:
            node['else_block'] = True
        return node

    # ---- Block ----
    def parse_block(self) -> list[Node]:
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
    def parse_for(self) -> Node:
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
    def parse_while(self) -> Node:
        self.expect(TT_KEYWORD, 'while')
        cond = self.parse_expression()
        block = self.peek() == (TT_KEYWORD, 'begin')
        if block:
            body = self.parse_block()
        else:
            stmt = self.parse_statement()
            body = [stmt] if stmt else []
        node: Node = {'type': 'while', 'cond': cond, 'body': body}
        if block:
            node['block'] = True
        return node

    # ---- Repeat..Until ----
    def parse_repeat_until(self) -> Node:
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
    def parse_once(self) -> Node:
        self.expect(TT_KEYWORD, 'once')
        block = self.peek() == (TT_KEYWORD, 'begin')
        if block:
            body = self.parse_block()
        else:
            stmt = self.parse_statement()
            body = [stmt] if stmt else []
        node: Node = {'type': 'once', 'body': body}
        if block:
            node['block'] = True
        return node

    # ---- Switch/Case ----
    def parse_switch(self) -> Node:
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
    def parse_attribute(self) -> Node:
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

    def parse_array_decl(self) -> Node:
        self.advance()  # consume 'arrays'/'array'
        self.match(TT_OP, ':')  # optional colon
        decls = []
        while True:
            # IntraBarPersist modifier may precede an array name too; retain it
            # rather than choking (codegen ignores the flag).
            intrabar = self.match(TT_KEYWORD, 'intrabarpersist') is not None
            name_index = self.pos
            name = self.expect(TT_IDENT)[1]
            self._user_names.add(name)  # declared names shadow reserved keywords
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
    def parse_commentary(self) -> Node:
        word = self.advance()[1]  # 'commentary' or 'commentarycl'
        self.expect(TT_LPAREN)
        msg = self.parse_expression()
        extra = []
        while self.match(TT_COMMA):
            extra.append(self.parse_expression())
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        node: Node = {'type': 'commentary', 'msg': msg}
        if extra:
            # Kept as separate arguments, not a `+` chain: MultiCharts
            # accepts numbers here, and "x=" + Close is not valid.
            node['extra_args'] = extra
        if word == 'commentarycl':
            node['cl'] = True
        return node

    def parse_commentary_block(self) -> Node:
        begin_index = self.pos
        self.advance()  # '#begincmtry'
        stmts = []
        while self.peek() != (TT_IDENT, '#end') and not self.at_end():
            if self.match(TT_SEMICOLON):
                continue
            stmt = self.parse_statement()
            if stmt:
                stmts.append(stmt)
        if self.peek() != (TT_IDENT, '#end'):
            self._err("expected '#End' to close '#BeginCmtry'",
                      hint="add an '#End;' to close this commentary block",
                      tok_index=begin_index)
        self.advance()
        self.match(TT_SEMICOLON)
        return {'type': 'commentary_block', 'body': stmts}

    # ---- Order ----
    def parse_order(self) -> Node:
        action = self.advance()[1]  # buy/sell/sellshort/buytocover
        # Documented two-word EL forms: `Sell Short` == SellShort and
        # `Buy To Cover` == BuyToCover (unless the second word is a declared name).
        if action == 'sell' and self.peek()[1] == 'short' and 'short' not in self._user_names:
            self.advance()
            action = 'sellshort'
        elif (action == 'buy' and self.peek()[1] == 'to'
              and self.pos + 1 < len(self.tokens)
              and self.tokens[self.pos + 1][1] == 'cover'):
            self.advance()
            self.advance()
            action = 'buytocover'
        label: Union[str, Node] = ''
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
                elif self.peek()[0] == TT_IDENT:
                    # A stray word after the price (e.g. an unsupported unit
                    # like `2 Ticks`): name it rather than misreporting the
                    # trailing `stop`/`limit` as unimplemented.
                    self._err(f"unexpected {self._describe(self.peek())} "
                              "after the order price")
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
    def parse_risk_func(self) -> Node:
        name = self.advance()[1]
        if name == 'setexitonclose':
            # No args
            if self.match(TT_LPAREN):
                self.expect(TT_RPAREN)
            self.match(TT_SEMICOLON)
            return {'type': 'risk', 'func': name, 'arg': None}
        elif name in ('setpercenttrailing', 'setpercenttrailing_pt'):
            # Two-arg form: SetPercentTrailing(Profit, Pct) / SetPercentTrailing_pt(Points, Pct)
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
    def parse_plot(self) -> Node:
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
    def parse_alert(self) -> Node:
        self.advance()  # 'alert'
        self.expect(TT_LPAREN)
        msg = self.parse_expression()
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        return {'type': 'alert', 'msg': msg}

    # ---- Print ----
    def parse_print_stmt(self) -> Node:
        self.advance()  # 'print'
        self.expect(TT_LPAREN)
        args = [self.parse_expression()]
        while self.match(TT_COMMA):
            args.append(self.parse_expression())
        self.expect(TT_RPAREN)
        self.match(TT_SEMICOLON)
        return {'type': 'print', 'args': args}

    # ---- Assignment or Expression Statement ----
    def parse_assignment_or_expr(self) -> Node:
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
