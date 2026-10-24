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

import copy
import re
from collections import OrderedDict
from typing import Any, NoReturn, Optional

from . import lexer as _lexer
from ._parser_expr import _ExpressionsMixin
from ._parser_stmts import _reconstruct_tokens, _StatementsMixin  # noqa: F401  (re-exported)
from ._types import Node, Token
from .lexer import (  # noqa: F401  (re-exported)
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
    TT_STRING,
    PLSyntaxError,
    collect_comments,
    source_line_at,
    tokenise,
)
from .parser_tables import (  # noqa: F401  (re-exported)
    _PRICE_ABBREVIATIONS,
    _SYMBOL_INFO_KEYWORDS,
    BUILTIN_ARITY,
    BUILTIN_FUNC_MAP,
    COLOR_CONSTANTS,
    DATA_SERIES,
    ORDER_ACTIONS,
    PERIOD_FUNCS,
    PLOT_NAMES,
    RISK_FUNCS,
    STYLE_CONSTANTS,
)
from .semantic import (  # noqa: F401  (re-exported)
    _BARE_COMPUTED,
    _RESERVED_IDENTS,
    _SCRATCH_VARS,
    _SERIES_PARAMS,
    collect_declared,
    is_known_name,
    semantic_check,
)

_DECL_KEYWORDS = frozenset(('inputs', 'input', 'variables', 'variable', 'vars',
                            'var', 'arrays', 'array'))


class Parser(_StatementsMixin, _ExpressionsMixin):
    def __init__(self, tokens: list[Token], positions: Optional[list[tuple[int, int]]] = None,
                 source: str = '') -> None:
        self.tokens = tokens
        self.positions = positions or []
        self.source = source
        self.pos = 0
        # Character offset of the start of each 1-based source line, so a token's
        # (line, col) position can be mapped back to the verbatim source text of an
        # identifier (the lexer lowercases idents; the ORIGINAL spelling is recovered
        # here per-occurrence for the '_ident' side-channel — see _spelling_at).
        self._line_starts: Optional[list[int]] = None
        # Inputs/Vars/Arrays declared so far. A declared name shadows a reserved
        # position/MC keyword of the same spelling (an input `ExitTime`), as in MC.
        self._user_names: set[str] = set()
        # Every Inputs/Vars/Arrays name in the program, collected up front so a
        # declaration later in the source still shadows a price abbreviation.
        self._declared_names = self._collect_declared_names()

    def _collect_declared_names(self) -> set[str]:
        """Cheap token pre-pass: the names declared by every Inputs/Vars/Arrays
        statement (identifiers at nesting depth 0 that start a comma-separated
        item, up to the terminating semicolon)."""
        names: set[str] = set()
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

    def _spelling_at(self, tok_index: int) -> Optional[str]:
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

    def _ident_spelling(self, name: str, tok_index: int) -> Optional[str]:
        """The original spelling to stamp under '_ident' for a USER identifier token,
        or None if it should not be stamped: builtins/keywords keep their canonical
        casing (tools/pl_signatures.jsonl) so they are excluded, and an already-
        lowercase spelling (== the parser's `name`) is redundant and omitted. Keeping
        this to genuine, differently-cased user identifiers means the forward output is
        byte-identical (codegen ignores '_ident') and the reparse-fixpoint holds."""
        if is_known_name(name, frozenset()):
            return None
        spelling = self._spelling_at(tok_index)
        if spelling is None or spelling == name:
            return None
        return spelling

    def peek(self) -> Token:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (TT_EOF, '')

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _pos_of(self, tok_index: int) -> tuple[int, int]:
        """(line, col) for the token at tok_index (clamped to the position list)."""
        if not self.positions:
            return (0, 0)
        idx = min(max(tok_index, 0), len(self.positions) - 1)
        return self.positions[idx]

    def _err(self, message: str, hint: str = '', tok_index: Optional[int] = None) -> NoReturn:
        """Raise a PLSyntaxError located at the given (default: current) token."""
        if tok_index is None:
            tok_index = self.pos
        line, col = self._pos_of(tok_index)
        raise PLSyntaxError(message, line, col,
                            source_line_at(self.source, line), hint)

    @staticmethod
    def _describe(tok: Token) -> str:
        """Human-readable description of a token for error messages."""
        tt, val = tok
        if tt == TT_EOF:
            return 'end of input'
        if tt in (TT_KEYWORD, TT_IDENT):
            return f"'{val}'"
        if val:
            return f"'{val}'"
        return tt

    def expect(self, tt: str, val: Any = None) -> Token:
        tok = self.peek()
        if tok[0] != tt or (val is not None and tok[1] != val):
            want = repr(val) if val is not None else tt
            self._err(
                f"expected {want} but found {self._describe(tok)}",
                hint=f"insert {want} here" if val is not None else '')
        return self.advance()

    def match(self, tt: str, val: Any = None) -> Optional[Token]:
        tok = self.peek()
        if tok[0] == tt and (val is None or tok[1] == val):
            return self.advance()
        return None

    def at_end(self) -> bool:
        return self.peek()[0] == TT_EOF

    # ---- Program ----
    def parse_program(self) -> Node:
        stmts: list[Node] = []
        while not self.at_end():
            # skip stray semicolons
            if self.match(TT_SEMICOLON):
                continue
            stmt = self.parse_statement()
            if stmt:
                stmts.append(stmt)
        return {'type': 'program', 'body': stmts}

    # ---- Statement ----
    def parse_statement(self) -> Optional[Node]:
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

    def _parse_statement_dispatch(self) -> Optional[Node]:
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

        # Commentary / CommentaryCL. Extra arguments go in extra_args.
        if tt == TT_IDENT and val in ('commentary', 'commentarycl'):
            return self.parse_commentary()

        # #BeginCmtry … #End. PowerBridge has no commentary-on mode, so the
        # block is kept in the AST and then skipped.
        if tt == TT_IDENT and val == '#begincmtry':
            return self.parse_commentary_block()

        # Print / PrintLog
        if tt == TT_IDENT and val in ('print', 'printlog'):
            return self.parse_print_stmt()

        # Assignment or expression statement
        return self.parse_assignment_or_expr()


def parse(tokens: list[Token], positions: Optional[list[tuple[int, int]]] = None,
          source: str = '') -> Node:
    """Parse token list into AST, then run the semantic validation pass.

    `positions`/`source` enable EL-aware (line:col + caret) error messages and
    the post-parse semantic checks. Arity errors raise; undefined-identifier
    findings are collected and attached to the AST under '_warnings' (they do
    not block valid code from transpiling).
    """
    return _parse(tokens, positions, source,
                  collect_comments(source) if source else [])


def _parse(tokens: list[Token], positions: Optional[list[tuple[int, int]]], source: str,
           comments: list[dict[str, Any]]) -> Node:
    """`parse` with the comment side-list supplied (``collect_comments(source)``,
    or what the `tokenise(source)` that produced `tokens` left behind)."""
    p = Parser(tokens, positions=positions, source=source)
    ast = p.parse_program()
    # Attach the verbatim comment side-channel. Derived purely from `source`
    # (comments are stripped from the token stream, so this is additive and never
    # affects behaviour). Empty when no source text was supplied.
    ast['_comments'] = comments if source else []
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


# One lex and one parse per distinct source text. A run reaches the front end
# from several places (lookback scans, the native serialiser, transpile, the
# strict pre-pass of --partial); all are pure functions of the source, so they
# share these results. Failures are never cached: a source that does not lex
# or parse raises afresh, with the same error, at every call.
_SOURCE_CACHE_SIZE = 8
_source_cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _source_entry(source: str) -> dict[str, Any]:
    entry = _source_cache.get(source)
    if entry is None:
        tokens, positions = tokenise(source)
        entry = {'tokens': tokens, 'positions': positions,
                 'comments': [dict(c) for c in _lexer.last_comments]}
        _source_cache[source] = entry
        if len(_source_cache) > _SOURCE_CACHE_SIZE:
            _source_cache.popitem(last=False)
    else:
        _source_cache.move_to_end(source)
        # Leave the lexer side channel as a fresh tokenise(source) would.
        _lexer.last_comments[:] = [dict(c) for c in entry['comments']]
    return entry


def tokenise_source(source: str) -> tuple[list[Token], list[tuple[int, int]]]:
    """``tokenise(source)``, lexed at most once per distinct source text."""
    entry = _source_entry(source)
    return list(entry['tokens']), list(entry['positions'])


def parse_source(source: str) -> Node:
    """``parse(*tokenise(source), source=source)``, lexed and parsed at most once
    per distinct source text. Returns a private deep copy of the AST, so callers
    may mutate it."""
    entry = _source_entry(source)
    if 'ast' not in entry:
        entry['ast'] = _parse(list(entry['tokens']), list(entry['positions']),
                              source, [dict(c) for c in entry['comments']])
    return copy.deepcopy(entry['ast'])


def clear_source_cache() -> None:
    """Forget every cached lex/parse (for tests that patch the front end)."""
    _source_cache.clear()
