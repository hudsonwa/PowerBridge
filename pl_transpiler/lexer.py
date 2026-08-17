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

"""PowerLanguage Lexer — tokenises PL source into a flat token list.

In addition to the token list, the lexer tracks the (line, col) source position of
every token (1-based) so the parser/semantic pass can produce EL-aware error
messages an LLM can self-correct from. See PLSyntaxError for the rendered format.
"""
import re

# Token types
TT_KEYWORD   = 'KEYWORD'
TT_IDENT     = 'IDENT'
TT_NUMBER    = 'NUMBER'
TT_STRING    = 'STRING'
TT_OP        = 'OP'
TT_LBRACKET  = 'LBRACKET'
TT_RBRACKET  = 'RBRACKET'
TT_LPAREN    = 'LPAREN'
TT_RPAREN    = 'RPAREN'
TT_SEMICOLON = 'SEMICOLON'
TT_COMMA     = 'COMMA'
TT_NEWLINE   = 'NEWLINE'
TT_EOF       = 'EOF'
TT_COMMENT   = 'COMMENT'

# Side-channel: the verbatim comments stripped by the most recent tokenise() call.
# Comments ride here (NOT the token stream), so the token stream — and therefore all
# forward-transpiled behaviour — is byte-identical whether or not comments are read.
# Each entry: {'text': verbatim incl. delimiters, 'line', 'col' (1-based), 'trailing':
# bool (True iff non-whitespace precedes the comment on its line)}. Consumers should
# prefer collect_comments(source) (a pure function of source) over reading this global.
last_comments = []

KEYWORDS = {
    'if','then','else','begin','end','for','to','downto','next',
    'while','do','repeat','until','break','continue','return','abort','switch','case',
    'default','once','inputs','input','variables','variable','vars','var',
    'arrays','array','intrabarpersist','and','or','not','mod',
    'true','false','crossesabove','crossesbelow',
}


class PLSyntaxError(SyntaxError):
    """An EasyLanguage syntax/semantic error located at a source line:col.

    Renders the 1-based line:col, the offending source line, a caret '^' under
    the offending column, and a short hint — formatted to be readable for an LLM
    so it can self-correct the generated EasyLanguage.
    """

    def __init__(self, message, line, col, source_line='', hint=''):
        self.pl_message = message
        self.line = line
        self.col = col
        self.source_line = source_line
        self.hint = hint
        super().__init__(self._render())

    def _render(self):
        loc = f"line {self.line}:{self.col}" if self.line else "end of input"
        parts = [f"EasyLanguage syntax error at {loc}: {self.pl_message}"]
        if self.source_line:
            # Show the offending line with a caret under the offending column.
            gutter = f"{self.line:>4} | "
            parts.append(gutter + self.source_line.rstrip('\r\n'))
            caret_pad = ' ' * (len(gutter) + max(0, self.col - 1))
            parts.append(caret_pad + '^')
        if self.hint:
            parts.append(f"hint: {self.hint}")
        return "\n".join(parts)


def _line_col(source, offset):
    """Convert a 0-based character offset into 1-based (line, col)."""
    line = source.count('\n', 0, offset) + 1
    last_nl = source.rfind('\n', 0, offset)
    col = offset - last_nl  # offset - last_nl gives 1-based col when last_nl is the index of '\n'
    return line, col


def tokenise(source: str):
    """Tokenise PL source. PL is case-insensitive — all tokens lowercased.

    Returns (tokens, positions) where `tokens` is a list of (type, value) pairs
    (unchanged from the historical contract) and `positions` is a parallel list
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
                    line, col, _source_line(src, line),
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
                    line, col, _source_line(src, line),
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
                # Two-word keyword: "crosses above" / "crosses below"
                j = i + m.end()
                while j < len(src) and src[j] in ' \t\r\n':
                    j += 1
                m2 = re.match(r'[A-Za-z_#][A-Za-z0-9_.]*', src[j:])
                if m2:
                    next_word = m2.group().lower()
                    if next_word == 'above':
                        emit((TT_KEYWORD, 'crossesabove'), start)
                        i = j + m2.end()
                        continue
                    elif next_word == 'below':
                        emit((TT_KEYWORD, 'crossesbelow'), start)
                        i = j + m2.end()
                        continue
                # Not followed by above/below; emit as regular ident
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
                line, col, _source_line(src, line),
                hint=("character not part of EasyLanguage; remove it or put it "
                      "inside a comment or string"))
        i += 1

    tokens.append((TT_EOF, ''))
    positions.append(_line_col(src, len(src)))
    # Expose the collected comments on the side channel (does not alter the return
    # contract — every existing caller unpacks exactly (tokens, positions)).
    last_comments.clear()
    last_comments.extend(comments)
    return tokens, positions


def collect_comments(source: str):
    """Verbatim comment side-list for `source` (a pure function of the text).

    Runs the lexer purely for its comment side-channel and returns a fresh copy of
    the collected comments (see `last_comments`). The parser uses this to attach
    `_comments` to the program node without any change to the token stream."""
    tokenise(source)
    return [dict(c) for c in last_comments]


def _source_line(source, line):
    """Return the 1-based `line`-th line of source (without trailing newline)."""
    if line < 1:
        return ''
    lines = source.split('\n')
    if line <= len(lines):
        return lines[line - 1]
    return ''
