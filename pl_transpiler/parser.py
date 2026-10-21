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

from .parser_tables import (  # noqa: F401  (re-exported)
    ORDER_ACTIONS, RISK_FUNCS, PLOT_NAMES, DATA_SERIES, COLOR_CONSTANTS,
    STYLE_CONSTANTS, PERIOD_FUNCS, BUILTIN_ARITY, BUILTIN_FUNC_MAP,
)
from .semantic import (  # noqa: F401  (re-exported)
    _SERIES_PARAMS, _BARE_COMPUTED, _SCRATCH_VARS, _RESERVED_IDENTS,
    _collect_declared, _is_known_name, semantic_check,
)


# Instrument properties read from the run's InstrumentConfig (via kwargs).
_SYMBOL_INFO_KEYWORDS = frozenset((
    'symbol', 'symbolname', 'description', 'category',
    'symbolcurrencycode', 'expirationdate',
))


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


# EL price-series abbreviations (see Parser.parse_primary).
_PRICE_ABBREVIATIONS = {'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close',
                        'v': 'volume', 'i': 'openint'}
_DECL_KEYWORDS = frozenset(('inputs', 'input', 'variables', 'variable', 'vars',
                            'var', 'arrays', 'array'))


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
        # Inputs/Vars/Arrays declared so far. A declared name shadows a reserved
        # position/MC keyword of the same spelling (an input `ExitTime`), as in MC.
        self._user_names = set()
        # Every Inputs/Vars/Arrays name in the program, collected up front so a
        # declaration later in the source still shadows a price abbreviation.
        self._declared_names = self._collect_declared_names()

    def _collect_declared_names(self):
        """Cheap token pre-pass: the names declared by every Inputs/Vars/Arrays
        statement (identifiers at nesting depth 0 that start a comma-separated
        item, up to the terminating semicolon)."""
        names = set()
        toks = self.tokens
        i, n = 0, len(toks)
        while i < n:
            tt, val = toks[i][0], toks[i][1]
            i += 1
            if tt != TT_KEYWORD or val not in _DECL_KEYWORDS:
                continue
            if i < n and toks[i][0] == TT_OP and toks[i][1] == ':':
                i += 1
            depth, expect_name = 0, True
            while i < n and not (depth == 0 and toks[i][0] == TT_SEMICOLON):
                tt, val = toks[i][0], toks[i][1]
                if tt in (TT_LPAREN, TT_LBRACKET):
                    depth += 1
                elif tt in (TT_RPAREN, TT_RBRACKET):
                    depth -= 1
                elif depth == 0 and tt == TT_COMMA:
                    expect_name = True
                elif expect_name and tt == TT_IDENT:
                    names.add(val)
                    expect_name = False
                i += 1
        return names

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
        # NOTE on the key name: a dedicated `_stmt_line` key was considered, but a
        # *new* diverging key would redden `verify_roundtrip.py --fixpoint`
        # (parse(src) and parse(emit_el(...)) land statements on different absolute
        # lines, so any line-valued key that survives the fixpoint's `_strip`
        # diverges) and would need a new entry in that gate's ignore set.
        # `_line` is ALREADY in the fixpoint + similarity ignore sets and already means "the source line of
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
    def parse_input_decl(self):
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

        # Price abbreviations O/H/L/C/V/I = Open/High/Low/Close/Volume/OpenInt.
        # Documented behaviour: a declared input/variable of the same name
        # shadows the abbreviation, and `I(...)` stays a function call.
        if (tt == TT_IDENT and val in _PRICE_ABBREVIATIONS
                and val not in self._declared_names
                and not (self.pos + 1 < len(self.tokens)
                         and self.tokens[self.pos + 1][0] == TT_LPAREN)):
            # Build the long-form node; the token keeps its original spelling.
            tt, val = TT_IDENT, _PRICE_ABBREVIATIONS[val]

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
        if tt == TT_IDENT and val in _POSITION_KEYWORDS and not (
                val in self._user_names and not (
                    self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1][0] == TT_LPAREN)):
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
            'symbol': 'symbol', 'symbolname': 'symbolname',
            'description': 'description', 'category': 'category',
            'symbolcurrencycode': 'symbolcurrencycode',
            'expirationdate': 'expirationdate',
        }
        if tt == TT_IDENT and val in _SYMBOL_INFO_KEYWORDS:
            # Symbol-info properties take an optional data-stream argument,
            # e.g. SymbolName(1). Runs have one instrument, so it is evaluated
            # for its references and otherwise ignored.
            self.advance()
            if self.peek()[0] == TT_LPAREN:
                self.advance()
                if self.peek()[0] != TT_RPAREN:
                    self.parse_expression()
                self.expect(TT_RPAREN)
            return {'type': 'position_kw', 'kwarg': _DATA_SERIES_KEYWORDS[val]}
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
        if tt == TT_IDENT and val in _MC_KEYWORDS and val not in self._user_names:
            self.advance()
            return {'type': 'mc_kw', 'kwarg': _MC_KEYWORDS[val]}

        # CrossesAbove / CrossesBelow as keywords
        if tt == TT_KEYWORD and val in ('crossesabove', 'crossesbelow'):
            return self._parse_func_call()

        # Mod as a function form: Mod(a, b) -> a % b (same node the infix `mod` produces).
        # NOTE: matches Python % for non-negative operands (EL usage here); negative-operand
        # EL Mod semantics are a runtime concern.
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
            return self._parse_ident_or_call(val)

        self._err(
            f"unexpected {self._describe(tok)} where an expression was expected",
            hint="check for a missing operator, operand, or a typo'd keyword")

    def _parse_ident_or_call(self, name=None):
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


def parse(tokens, positions=None, source=''):
    """Parse token list into AST, then run the semantic validation pass.

    `positions`/`source` enable EL-aware (line:col + caret) error messages and
    the post-parse semantic checks. Arity errors raise; undefined-identifier
    findings are collected and attached to the AST under '_warnings' (they do
    not block valid code from transpiling).
    """
    p = Parser(tokens, positions=positions, source=source)
    ast = p.parse_program()
    # Attach the verbatim comment side-channel. Derived purely from `source`
    # (comments are stripped from the token stream, so this is additive and never
    # affects behaviour). Empty when no source text was supplied.
    ast['_comments'] = collect_comments(source) if source else []
    # Leading blank lines at the very top of the source (preserved across round trips). Counted
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
