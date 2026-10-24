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

"""Frozen copy of the quadratic lexer from before #180, used only as a reference.

tests/test_lexer_linear.py checks that pl_transpiler.lexer.tokenise gives exactly
the same tokens, positions, comments and errors as this copy. Do not edit it.
The only change from the original is that the comments go to the returned
`comments` list instead of the module-level side channel.
"""
import re

from pl_transpiler.lexer import (  # noqa: F401
    KEYWORDS,
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
    source_line_at,
)


def _line_col(source, offset):
    """Convert a 0-based character offset into 1-based (line, col)."""
    line = source.count('\n', 0, offset) + 1
    last_nl = source.rfind('\n', 0, offset)
    col = offset - last_nl  # offset - last_nl gives 1-based col when last_nl is the index of '\n'
    return line, col


def tokenise(source: str):
    """Tokenise PL source. PL is case-insensitive — all tokens lowercased.

    Returns (tokens, positions) where `tokens` is a list of (type, value) pairs
    and `positions` is a parallel list
    of (line, col) 1-based source positions, one per token (the EOF token gets
    the position just past the end of the source).
    """
    tokens = []
    positions = []
    comments = []
    i = 0
    src = source

    def emit(tok, start):
        tokens.append(tok)
        positions.append(_line_col(src, start))

    def _add_comment(start, end):
        # Collect a stripped comment verbatim (delimiters included) into the side
        # channel. `trailing` iff the comment is preceded by non-whitespace on its
        # own line. This never touches `tokens`/`positions`.
        line, col = _line_col(src, start)
        line_start = src.rfind('\n', 0, start) + 1
        comments.append({
            'text': src[start:end],
            'line': line,
            'col': col,
            'trailing': src[line_start:start].strip() != '',
        })

    while i < len(src):
        # Skip whitespace
        if src[i] in ' \t\r\n':
            i += 1
            continue

        # Curly-brace comments { ... } (handle nesting)
        if src[i] == '{':
            depth = 1
            j = i + 1
            while j < len(src) and depth > 0:
                if src[j] == '{':
                    depth += 1
                elif src[j] == '}':
                    depth -= 1
                j += 1
            if depth > 0:
                line, col = _line_col(src, i)
                raise PLSyntaxError(
                    "unterminated comment '{' — no matching '}'",
                    line, col, source_line_at(src, line),
                    hint="close the comment with '}'")
            _add_comment(i, j)
            i = j
            continue

        # Line comments //
        if src[i:i+2] == '//':
            end = src.find('\n', i)
            if end == -1:
                end = len(src)
            _add_comment(i, end)
            i = end
            continue

        # String literals
        if src[i] == '"':
            j = i + 1
            while j < len(src) and src[j] != '"':
                # PL string literals do not span lines.
                if src[j] == '\n':
                    break
                j += 1
            if j >= len(src) or src[j] != '"':
                line, col = _line_col(src, i)
                raise PLSyntaxError(
                    'unterminated string literal',
                    line, col, source_line_at(src, line),
                    hint='close the string with a matching double-quote (")')
            emit((TT_STRING, src[i+1:j]), i)
            i = j + 1
            continue

        # Numbers
        m = re.match(r'\d+(\.\d+)?', src[i:])
        if m:
            emit((TT_NUMBER, m.group()), i)
            i += m.end()
            continue

        # Identifiers and keywords
        m = re.match(r'[A-Za-z_#][A-Za-z0-9_.]*', src[i:])
        if m:
            start = i
            word = m.group().lower()
            if word in KEYWORDS:
                emit((TT_KEYWORD, word), start)
            elif word == 'crosses':
                # Two-word keyword: "crosses above" / "crosses below";
                # "over" / "under" are synonyms for "above" / "below".
                j = i + m.end()
                while j < len(src) and src[j] in ' \t\r\n':
                    j += 1
                m2 = re.match(r'[A-Za-z_#][A-Za-z0-9_.]*', src[j:])
                if m2:
                    next_word = m2.group().lower()
                    if next_word in ('above', 'over'):
                        emit((TT_KEYWORD, 'crossesabove'), start)
                        i = j + m2.end()
                        continue
                    elif next_word in ('below', 'under'):
                        emit((TT_KEYWORD, 'crossesbelow'), start)
                        i = j + m2.end()
                        continue
                # Not followed by above/below/over/under; emit as regular ident
                emit((TT_IDENT, word), start)
            else:
                emit((TT_IDENT, word), start)
            i += m.end()
            continue

        # Operators and punctuation
        two = src[i:i+2]
        if two in ('>=', '<=', '<>', ':=', '..'):
            emit((TT_OP, two), i)
            i += 2
            continue

        ch = src[i]
        if ch in '+-*/=<>':
            emit((TT_OP, ch), i)
        elif ch == '[':
            emit((TT_LBRACKET, ch), i)
        elif ch == ']':
            emit((TT_RBRACKET, ch), i)
        elif ch == '(':
            emit((TT_LPAREN, ch), i)
        elif ch == ')':
            emit((TT_RPAREN, ch), i)
        elif ch == ';':
            emit((TT_SEMICOLON, ch), i)
        elif ch == ',':
            emit((TT_COMMA, ch), i)
        elif ch == ':':
            emit((TT_OP, ch), i)
        else:
            # Unknown character: fail loud rather than silently dropping it, so
            # garbage can never transpile to plausible-but-wrong EL.
            line, col = _line_col(src, i)
            raise PLSyntaxError(
                f"unexpected character {ch!r}",
                line, col, source_line_at(src, line),
                hint=("character not part of EasyLanguage; remove it or put it "
                      "inside a comment or string"))
        i += 1

    tokens.append((TT_EOF, ''))
    positions.append(_line_col(src, len(src)))
    return tokens, positions, comments
