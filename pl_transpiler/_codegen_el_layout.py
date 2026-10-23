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


"""Leaf text-layout helpers of `codegen_el` (indentation, blank lines, line
wrapping, comment anchoring and compact-form tests). None of them emits a
statement or expression, so this module never imports codegen_el."""


# --- statement layout ------------------------------------------------------
_TAB = "\t"


# Declaration statement kinds — used for blank-line fallback (section break after
# the declaration section) and one-per-line block formatting.
_DECL_STMT_TYPES = {"var_decl", "input_decl", "array_decl"}


# Long lines are wrapped so no emitted line exceeds this many characters; 100
# leaves head-room under the 120-char --gate-fmt ceiling (tabs count as 1 char,
# matching the gate's len()-based measurement).
_WRAP_WIDTH = 100


def _ind(level):
    return _TAB * level


def _max_line(node):
    """Largest `_line` value anywhere in `node`'s subtree, or None.

    Statement nodes carry `_line` (parser.parse_statement); this returns the last
    original source line the statement occupied (its deepest inner statement), so
    blank-line preservation can measure the gap to the NEXT statement's start line
    rather than mis-measuring across a multi-line block's own span."""
    best = None
    if isinstance(node, dict):
        v = node.get("_line")
        if isinstance(v, int):
            best = v
        for k, val in node.items():
            if k == "_line":
                continue
            m = _max_line(val)
            if m is not None and (best is None or m > best):
                best = m
    elif isinstance(node, list):
        for it in node:
            m = _max_line(it)
            if m is not None and (best is None or m > best):
                best = m
    return best


def _blank_between(prev, cur):
    """True iff one blank line should separate consecutive statements `prev`/`cur`.

    Round-trip case (both carry `_line`): a blank line existed in the original iff
    the next statement starts >= 2 lines after the previous statement ends.
    Fallback (line metadata absent, e.g. a py_front-synthesized AST): a section
    break after the declaration section — a blank before the first non-declaration
    statement that follows a declaration."""
    pe = _max_line(prev)
    cl = cur.get("_line") if isinstance(cur, dict) else None
    if isinstance(pe, int) and isinstance(cl, int):
        return (cl - pe) >= 2
    if isinstance(prev, dict) and isinstance(cur, dict):
        return (prev.get("type") in _DECL_STMT_TYPES
                and cur.get("type") not in _DECL_STMT_TYPES)
    return False


def _emit_decl_block(keyword, parts, pad):
    """Emit an Inputs:/Vars:/Arrays: declaration block.

    A single declaration stays inline (`Keyword: Name( init ) ;`); a multi-item
    block puts the keyword on its own line and ONE declaration per tab-indented
    line, the last ending `;` (the reference-strategy house style)."""
    if len(parts) == 1:
        return f"{pad}{keyword}: {parts[0]} ;"
    inner = pad + _TAB
    lines = [f"{pad}{keyword}:"]
    for i, p in enumerate(parts):
        term = " ;" if i == len(parts) - 1 else ","
        lines.append(f"{inner}{p}{term}")
    return "\n".join(lines)


# --- long-line wrapping (whitespace-only; lexer-invisible) ------------------
def _split_atoms(s):
    """Split a line's content into space-separated atoms, keeping each quoted
    string (which may itself contain spaces) intact as ONE atom so a wrap never
    lands inside a string literal."""
    atoms, cur, i, n = [], [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 1
            if j < n:
                j += 1                       # include the closing quote
            cur.append(s[i:j])
            i = j
        elif c == " ":
            if cur:
                atoms.append("".join(cur))
                cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    if cur:
        atoms.append("".join(cur))
    return atoms


def _wrap_line(line, width=_WRAP_WIDTH):
    """Wrap one emitted line to <= width chars by breaking at inter-atom spaces
    (a newline where a space was is lexer-invisible, so re-lex is unchanged).
    Continuation lines get one extra tab of indent. A line with a single atom
    wider than `width` is emitted unbroken (there is no safe break point)."""
    if len(line) <= width:
        return [line]
    stripped = line.lstrip("\t")
    indent = line[:len(line) - len(stripped)]
    atoms = _split_atoms(stripped)
    if len(atoms) <= 1:
        return [line]
    cont = indent + _TAB
    out, cur, cur_indent, cur_len = [], [], indent, len(indent)
    for a in atoms:
        extra = len(a) + (1 if cur else 0)
        if cur and cur_len + extra > width:
            out.append(cur_indent + " ".join(cur))
            cur, cur_indent, cur_len = [a], cont, len(cont) + len(a)
        else:
            cur.append(a)
            cur_len += extra
    if cur:
        out.append(cur_indent + " ".join(cur))
    return out


def _comment_start(line):
    """Index where an EL comment (`{` block or `//` run) begins on `line`, respecting
    string literals so a `{`/`//` inside a "..." string is never mistaken for a
    comment; None if the line carries no comment. Used to keep wrapping away from
    comment text (a wrap inside a `//` run would terminate it early and change
    tokens)."""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 1
            i += 1
            continue
        if c == '{':
            return i
        if c == '/' and i + 1 < n and line[i + 1] == '/':
            return i
        i += 1
    return None


def _wrap_all(text):
    """Apply _wrap_line to every physical line of the emitted program, but NEVER wrap
    comment text: a `//` run must not be split (it would terminate early and
    reappear as code tokens), and comment text is emitted verbatim (no reflowing).
    Multi-line `{...}` blocks pass through untouched; a code line carrying a trailing
    comment has only its code prefix wrapped, the comment re-appended to the last
    wrapped segment."""
    out = []
    in_brace = False               # inside an unclosed multi-line `{ ... }` block
    for line in text.split("\n"):
        if in_brace:
            out.append(line)
            if '}' in line:
                in_brace = False
            continue
        cs = _comment_start(line)
        if cs is None:
            out.extend(_wrap_line(line))
            continue
        if line[cs] == '{' and '}' not in line[cs:]:
            out.append(line)       # opening line of a multi-line brace comment
            in_brace = True
            continue
        code = line[:cs].rstrip()
        comment = line[cs:]
        if not code:
            out.append(line)       # a whole-line comment (verbatim)
            continue
        wrapped = _wrap_line(code)
        wrapped[-1] = wrapped[-1] + " " + comment
        out.extend(wrapped)
    return "\n".join(out)


def _collect_line_stmts(body, out):
    """The statement nodes of `body`, in emission order, following ONLY statement-
    sequence positions (block bodies / else / switch cases / default).

    These are exactly the nodes emit_stmt is called on, so anchoring comments to them
    by object identity is the only anchoring that survives emission. Expression sub-
    nodes ALSO carry `_line` (e.g. an assignment's target ident), but they are emitted
    via emit_expr, which ignores the comment maps — anchoring a comment there would
    silently drop it. So we must descend through statement structure, not every node."""
    for stmt in body:
        if not isinstance(stmt, dict):
            continue
        if isinstance(stmt.get('_line'), int):
            out.append(stmt)
        t = stmt.get('type')
        if t == 'if':
            _collect_line_stmts(stmt.get('body') or [], out)
            if stmt.get('else_body') is not None:
                _collect_line_stmts(stmt['else_body'], out)
        elif t in ('for', 'while', 'repeat_until', 'once'):
            _collect_line_stmts(stmt.get('body') or [], out)
        elif t == 'switch':
            for case in stmt.get('cases') or []:
                _collect_line_stmts(case.get('body') or [], out)
            if stmt.get('default') is not None:
                _collect_line_stmts(stmt['default'], out)
    return out


def _anchor_comments(body, comments):
    """Distribute `comments` (each {'text','line','col','trailing'}) over the
    statements in `body`. Returns (header, leading_by_id, trailing_by_id, footer):

      * trailing comment on the same line as a statement -> that statement's trailing;
      * a comment before the earliest statement -> header (emitted first, verbatim);
      * otherwise the nearest FOLLOWING statement by line -> its leading;
      * a comment after the last statement -> footer (emitted last).

    When no statement carries a line (e.g. a py_front-synthesised AST), every comment
    falls to the header block — placement is lost but not one comment is dropped."""
    leading, trailing = {}, {}
    header, footer = [], []
    ordered = sorted(comments, key=lambda c: (c.get('line', 0), c.get('col', 0)))
    line_stmts = _collect_line_stmts(body, [])
    if not line_stmts:
        return [c['text'] for c in ordered], leading, trailing, footer
    min_l = min(s['_line'] for s in line_stmts)
    for c in ordered:
        cl = c.get('line', 0)
        text = c['text']
        if c.get('trailing'):
            same = [s for s in line_stmts if s['_line'] == cl]
            if same:
                trailing.setdefault(id(same[-1]), []).append(text)
                continue
        if cl < min_l:
            header.append(text)
            continue
        following = [s for s in line_stmts if s['_line'] > cl]
        if following:
            node = min(following, key=lambda s: s['_line'])
            leading.setdefault(id(node), []).append(text)
        else:
            footer.append(text)
    return header, leading, trailing, footer


def _comment_lines(texts, pad):
    """Verbatim comment `texts` as physical output lines at indentation `pad`. A
    multi-line `{...}` block keeps its internal newlines (each physical line padded);
    text is never reflowed.

    IDEMPOTENCE: a physical line that ALREADY begins with `pad` has that one
    `pad` stripped before `pad` is re-applied, so re-emitting an already-emitted comment
    is a fixed point. Without this, each round trip prepends another `pad` to a multi-
    line block's continuation lines (they are re-parsed WITH the emitted indent), and
    the text grows without bound — the exact non-convergence the cycles gate forbids.
    The Python leg splits a block into one recaptured entry per physical line, so the
    strip must apply to every line, not just continuations; a line's own content beyond
    `pad` (e.g. a hanging-indent alignment) is preserved."""
    out = []
    for text in texts:
        for phys in text.split('\n'):
            if pad and phys.startswith(pad):
                phys = phys[len(pad):]
            out.append(pad + phys)
    return out


# --- compact single-statement blocks --------------------------------------
# The parser retains whether the source wrote an explicit Begin/End for each body
# leg (then_block / else_block on `if`; block on for/while/once). A single-statement
# leg whose flag says NO Begin/End is emitted in the compact `If C Then <stmt>` form;
# multi-statement or flagged legs keep Begin/End. An AST synthesised by py_front
# carries no flag, so its single-statement legs default to compact.
def _dangles(stmt):
    """True if `stmt`, emitted per the compact rules below, ends in an open `Then`
    that a following `Else` would re-associate to (the dangling-else hazard). The
    reparse-fixpoint gate is the arbiter — when this is True the caller must keep
    Begin/End so the emitted text reparses to the SAME association."""
    if not isinstance(stmt, dict):
        return False
    t = stmt.get('type')
    if t == 'if':
        eb = stmt.get('else_body')
        if eb is None:
            return True                     # `If cond Then ...` grabs a following Else
        if _else_compact(stmt):
            return _dangles(eb[-1])         # danger propagates down the else chain
        return False                        # Else emitted as a closed Begin/End
    if t in ('for', 'while', 'once'):
        if _loop_compact(stmt):
            body = stmt.get('body') or []
            return _dangles(body[-1]) if body else False
        return False
    return False


def _is_comment_line(line):
    s = line.lstrip()
    return s.startswith('{') or s.startswith('//')


def _then_compact(node):
    body = node.get('body') or []
    if len(body) != 1 or node.get('then_block'):
        return False
    # A following Else must not be captured by a dangling `Then` inside the then leg.
    if node.get('else_body') is not None and _dangles(body[0]):
        return False
    return True


def _else_compact(node):
    eb = node.get('else_body')
    return eb is not None and len(eb) == 1 and not node.get('else_block')


def _loop_compact(node):
    body = node.get('body') or []
    return len(body) == 1 and not node.get('block')
