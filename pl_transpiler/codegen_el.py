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

"""codegen_el.py — REVERSE transpiler (GOAL R2b).

emit_el(ast) walks the parser's dict AST (the shared IR; node inventory in
transpiler/parser.py) and emits compile-safe PowerLanguage / EasyLanguage. It is
the mirror of the forward codegen: whatever `parse()` accepts, `emit_el` must be
able to reproduce as text that `parse()` re-accepts to the SAME AST (the
reparse-fixpoint), and that MultiCharts compiles (mc_ground_check exit-0).

Hard rules (grounded against the parser/lexer, non-negotiable):
  * emit_el takes the AST ONLY. It never reads source text, GT files, or any
    retention metadata beyond what the parser stored on the node.
  * NEVER emit '%' (the lexer has no '%'): binop op='%' emits infix ` mod `.
  * crossesabove/crossesbelow CALL nodes emit INFIX `crosses above/below`.
  * ident '_market_position' emits `MarketPosition` (the parser renames it inbound).
  * numeric literals are emitted VERBATIM from the stored original string.
  * strings are re-quoted verbatim; a value containing '"' or newline RAISES
    (EL strings have no escapes).
  * begin/end blocks are ALWAYS emitted for if/for/while/once bodies (the parser
    drops single-statement vs block; explicit blocks are canonical and safe).
  * orders are ALWAYS emitted in explicit form.
  * parenthesisation is re-derived from the PARSER'S precedence — it is the oracle.
  * any node kind / field that cannot be faithfully emitted RAISES ELEmitError
    (fail-loud; never emit plausible-but-wrong EL).

Canonical casing comes from pl_transpiler.tools/pl_signatures.jsonl. Casing is cosmetic (the
lexer lowercases everything, and mc_ground_check compares case-insensitively);
the lowercase fallback is always compile-safe.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SIG_PATH = os.path.join(_HERE, "tools", "pl_signatures.jsonl")

# DIALECT_VERSION of the EL surface this emitter targets. R4's py_front asserts a
# matching constant in the forward codegen; kept here as the reverse module's own
# stamp of the emission contract.
DIALECT_VERSION = "1.0"


class ELEmitError(Exception):
    """Raised when the AST holds a node kind / field the emitter cannot faithfully
    reproduce as compile-safe EL. Fail-loud: never emit a guess."""


def _eln(node):
    """`" at line N"` when the AST dict `node` carries an EL source line, else ``""``.
    Never fabricates a line — best-effort position info for error messages."""
    if isinstance(node, dict):
        ln = node.get('_line')
        if isinstance(ln, int):
            return f" at line {ln}"
    return ""


# --- canonical casing ------------------------------------------------------
def _load_sig_map():
    """lowercase keyword -> canonical-cased keyword, from pl_signatures.jsonl."""
    m = {}
    with open(_SIG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            kw = json.loads(line)["keyword"]
            m[kw.lower()] = kw
    return m


_SIG = _load_sig_map()


def _canon(name):
    """Canonical casing for a keyword/builtin; lowercase fallback is compile-safe."""
    return _SIG.get(name.lower(), name)


# --- reverse keyword maps (inverses of the parser's local kwarg tables) -----
# Copied verbatim from pl_transpiler/parser.py:parse_primary so the reverse map is
# an exact inverse. First EL spelling wins (the non-_checked / non-underscored
# variant), which reparses to the SAME kwarg — so the fixpoint holds.
_POSITION_KEYWORDS = {
    'entryprice': 'entry_price',
    'exitprice': 'exit_price',
    'positionprofit': 'position_profit',
    'barssinceentry': 'bars_since_entry',
    'barssinceexit': 'bars_since_exit',
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
    'lastbaronchartex': 'lastbaronchartex',
    'alertenabled': 'alert_enabled',
    'checkalert': 'check_alert',
    'maxcontracts': 'max_contracts',
    'currentcontracts': 'current_contracts',
    'maxentries': 'max_entries',
    'maxcontractsheld': 'maxcontractsheld',
    'maxiddrawdown': 'maxiddrawdown',
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
_MC_KEYWORDS = {
    'time_s': 'time_s',
    'bartype_ex': 'bartype_ex',
}


def _invert_first_wins(d):
    inv = {}
    for el_name, kwarg in d.items():
        inv.setdefault(kwarg, el_name)
    return inv


# kwarg -> canonical EL spelling. position_kw nodes draw from BOTH the position
# and data-series tables; mc_kw nodes from the mc table.
_INV_POSKW = {**_invert_first_wins(_DATA_SERIES_KEYWORDS),
              **_invert_first_wins(_POSITION_KEYWORDS)}
_INV_MCKW = _invert_first_wins(_MC_KEYWORDS)

# ident renames the parser applies inbound that must be reversed to the EL keyword
# (a plain re-emit of the internal name would reparse to a different node).
_IDENT_REVERSE = {'_market_position': 'MarketPosition'}

# order verb canonical spellings
_ORDER_ACTION = {
    'buy': 'Buy', 'sell': 'Sell',
    'sellshort': 'SellShort', 'buytocover': 'BuyToCover',
}

# binop op -> emitted operator word
_BINOP_WORD = {
    'or': 'or', 'and': 'and',
    '+': '+', '-': '-', '*': '*', '/': '/',
    '%': 'mod',   # NEVER '%': the lexer has no '%'; infix `mod` reparses to '%'
}


# --- precedence (the parser is the oracle) ---------------------------------
# Higher binds tighter. Mirrors parser.py: parse_or < parse_and < parse_not <
# parse_comparison < parse_addition < parse_multiplication < parse_unary <
# primary.
def _prec(node):
    if not isinstance(node, dict):
        return 100
    t = node.get('type')
    if t == 'binop':
        op = node.get('op')
        if op == 'or':
            return 1
        if op == 'and':
            return 2
        if op in ('+', '-'):
            return 5
        if op in ('*', '/', '%'):
            return 6
        raise ELEmitError(f"binop with unknown op {op!r}{_eln(node)}")
    if t == 'unaryop':
        return 3 if node.get('op') == 'not' else 7
    if t == 'compare':
        return 4
    if t == 'call' and node.get('name') in ('crossesabove', 'crossesbelow'):
        return 4  # emitted infix at comparison precedence
    return 100


def _operand(node, parent_prec, is_right):
    """Emit `node` as an operand under a parent of `parent_prec`, adding parens
    only where the parser's precedence would otherwise re-group it. Extra parens
    are always fixpoint-safe (the parser strips them), so this errs toward
    grouping when precedence ties on the right of a left-associative operator."""
    s = emit_expr(node)
    cp = _prec(node)
    if cp < parent_prec or (cp == parent_prec and is_right):
        return f"({s})"
    return s


# --- expression emission ---------------------------------------------------
def _emit_string(value):
    if '"' in value or '\n' in value or '\r' in value:
        raise ELEmitError(
            "string literal contains a double-quote or newline, which EL cannot "
            f"represent (no escapes): {value!r}")
    return f'"{value}"'


def emit_expr(node):
    """Emit an expression node as EL text (no surrounding parens; callers wrap)."""
    if not isinstance(node, dict):
        raise ELEmitError(f"expression node is not a dict: {node!r}")
    t = node.get('type')

    if t == 'number':
        sym = node.get('_symbol')
        if sym is not None:
            return _canon(sym)          # faithful color/style token name (R1)
        return node['value']            # VERBATIM original literal string

    if t == 'string':
        return _emit_string(node['value'])

    if t == 'boolean':
        return 'true' if node['value'] else 'false'

    if t == 'ident':
        name = node.get('name', '')
        if name in _IDENT_REVERSE:
            return _IDENT_REVERSE[name]
        ident = node.get('_ident')
        if ident is not None:               # user identifier's original casing (R8)
            return ident
        return _canon(name)

    if t == 'binop':
        op = node['op']
        word = _BINOP_WORD.get(op)
        if word is None:
            raise ELEmitError(f"binop with unknown op {op!r}{_eln(node)}")
        p = _prec(node)
        return f"{_operand(node['left'], p, False)} {word} " \
               f"{_operand(node['right'], p, True)}"

    if t == 'compare':
        p = 4
        return f"{_operand(node['left'], p, False)} {node['op']} " \
               f"{_operand(node['right'], p, True)}"

    if t == 'unaryop':
        op = node['op']
        p = _prec(node)
        if op == 'not':
            return f"not {_operand(node['operand'], p, True)}"
        if op == '-':
            return f"-{_operand(node['operand'], p, True)}"
        raise ELEmitError(f"unaryop with unknown op {op!r}{_eln(node)}")

    if t == 'call':
        name = node.get('name', '')
        args = node.get('args', [])
        if name in ('crossesabove', 'crossesbelow'):
            if len(args) != 2:
                raise ELEmitError(f"{name} expects 2 args, got {len(args)}{_eln(node)}")
            word = 'crosses above' if name == 'crossesabove' else 'crosses below'
            return f"{_operand(args[0], 4, False)} {word} " \
                   f"{_operand(args[1], 4, True)}"
        inner = ", ".join(emit_expr(a) for a in args)
        # A user-defined function call carries its original spelling in '_ident'
        # (the parser only stamps it on non-builtins); builtins keep canonical casing.
        fname = node.get('_ident') or _canon(name)
        return f"{fname}({inner})"

    if t == 'bar_ref':
        return f"{emit_expr(node['series'])}[{emit_expr(node['index'])}]"

    if t == 'position_kw':
        kw = node['kwarg']
        el = _INV_POSKW.get(kw)
        if el is None:
            raise ELEmitError(f"position_kw with unmappable kwarg {kw!r}{_eln(node)}")
        return _canon(el)

    if t == 'position_kw_call':
        kw = node['kwarg']
        el = _INV_POSKW.get(kw)
        if el is None:
            raise ELEmitError(f"position_kw_call with unmappable kwarg {kw!r}{_eln(node)}")
        inner = ", ".join(emit_expr(a) for a in node.get('args', []))
        return f"{_canon(el)}({inner})"

    if t == 'mc_kw':
        kw = node['kwarg']
        el = _INV_MCKW.get(kw)
        if el is None:
            raise ELEmitError(f"mc_kw with unmappable kwarg {kw!r}{_eln(node)}")
        return _canon(el)

    if t == 'data_of':
        return f"{_canon(node['series'])} of Data{node['data_num']}"

    raise ELEmitError(f"cannot emit expression node of type {t!r}: {node!r}{_eln(node)}")


# --- statement emission ----------------------------------------------------
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


def _emit_seq(stmts, level):
    """Emit a statement sequence as lines, inserting one blank line between
    consecutive statements per `_blank_between` (blank-line preservation)."""
    out = []
    prev = None
    for s in stmts:
        if prev is not None and _blank_between(prev, s):
            out.append("")
        out.append(emit_stmt(s, level))
        prev = s
    return out


def _emit_block(stmts, level):
    """Emit `stmts` as indented lines (one entry per statement, blank lines kept)."""
    return _emit_seq(stmts, level)


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
    comment text (Rcmt): a `//` run must not be split (it would terminate early and
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


def _emit_decl_init(init):
    return emit_expr(init)


# --- comment interleaving (Rcmt) -------------------------------------------
# Comments ride the AST's `_comments` side-list (attached by the parser). On emit
# they are anchored back to statements by their original source line so the
# readability of the source survives the round trip. Anchoring is keyed by object
# identity of the statement node via two per-emit maps set up in emit_el:
_LEADING = {}    # id(stmt) -> [verbatim comment text, ...]  (emitted on own lines before)
_TRAILING = {}   # id(stmt) -> [verbatim comment text, ...]  (appended to the stmt's line)


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

    IDEMPOTENCE (Rcyc): a physical line that ALREADY begins with `pad` has that one
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


# --- compact single-statement blocks (R8) ----------------------------------
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


def _inline_leg(stmt, level, prefix):
    """Emit `stmt` as the compact body of a leg: its text with the leading indent of
    its first line replaced by `<pad><prefix>` (e.g. `If C Then `). Comments anchored
    to `stmt` are preserved — leading comments emit on their own lines before the
    prefixed line; trailing comments are appended to the last line. Comments are
    stripped before the fixpoint compare, so relocating them here is fixpoint-safe;
    the cycles gate's anchor phase bounds any residual positional shift."""
    pad = _ind(level)
    core = _emit_stmt_core(stmt, level)
    plines = core.split('\n')
    # A compact compound leg (`If C Then <if/for/...>`) surfaces its first sub-leg's
    # leading comments as the core's opening physical line(s). Prefixing `If C Then `
    # onto a comment does NOT round-trip (on reparse the comment re-anchors to the
    # compound and the whole leg re-compacts), so hoist those comment lines above the
    # prefixed statement line — the form the reparse fixpoint settles on. They are
    # already emitted at this leg's `pad`, so no re-indentation is needed.
    hoist = []
    while len(plines) > 1 and _is_comment_line(plines[0]):
        hoist.append(plines.pop(0))
    first = plines[0]
    first = first[len(pad):] if first.startswith(pad) else first.lstrip(_TAB)
    plines[0] = f"{pad}{prefix}{first}"
    trail = _TRAILING.get(id(stmt))
    if trail:
        plines[-1] = plines[-1] + ' ' + ' '.join(trail)
    lead = _LEADING.get(id(stmt))
    out = _comment_lines(lead, pad) if lead else []
    out.extend(hoist)
    out.extend(plines)
    return out


def emit_stmt(node, level):
    """Emit a statement with any anchored comments interleaved: leading comments on
    their own lines before it, trailing comments appended to its final line."""
    text = _emit_stmt_core(node, level)
    lead = _LEADING.get(id(node))
    trail = _TRAILING.get(id(node))
    if not lead and not trail:
        return text
    pad = _ind(level)
    lines = _comment_lines(lead, pad) if lead else []
    body_lines = text.split('\n')
    if trail:
        body_lines[-1] = body_lines[-1] + ' ' + ' '.join(trail)
    lines.extend(body_lines)
    return '\n'.join(lines)


def _emit_stmt_core(node, level):
    """Emit a statement node as a fully-indented (possibly multi-line) string."""
    if not isinstance(node, dict):
        raise ELEmitError(f"statement node is not a dict: {node!r}")
    pad = _ind(level)
    t = node.get('type')

    if t == 'var_decl':
        parts = []
        for d in node['decls']:
            pre = "IntraBarPersist " if d.get('intrabar_persist') else ""
            name = d.get('_ident') or d['name']
            init = _emit_decl_init(d['init'])
            if d.get('data_ref') is not None:
                parts.append(f"{pre}{name}( {init}, {emit_expr(d['data_ref'])} )")
            else:
                parts.append(f"{pre}{name}( {init} )")
        return _emit_decl_block("Vars", parts, pad)

    if t == 'input_decl':
        parts = [f"{d.get('_ident') or d['name']}( {_emit_decl_init(d['default'])} )"
                 for d in node['decls']]
        return _emit_decl_block("Inputs", parts, pad)

    if t == 'array_decl':
        parts = []
        for d in node['decls']:
            pre = "IntraBarPersist " if d.get('intrabar_persist') else ""
            name = d.get('_ident') or d['name']
            size = "" if d.get('size') is None else emit_expr(d['size'])
            init = _emit_decl_init(d['init'])
            parts.append(f"{pre}{name}[{size}]( {init} )")
        return _emit_decl_block("Arrays", parts, pad)

    if t == 'assign':
        target = emit_expr(node['target'])
        # `=` for ident/bar_ref targets (statement-level assignment); `:=` when
        # the target is a call form (the parser only accepts `:=` there).
        op = ":=" if node['target'].get('type') == 'call' else "="
        return f"{pad}{target} {op} {emit_expr(node['value'])} ;"

    if t == 'expr_stmt':
        return f"{pad}{emit_expr(node['expr'])} ;"

    if t == 'if':
        cond = emit_expr(node['cond'])
        eb = node.get('else_body')
        # THEN leg: compact single statement, or explicit Begin/End.
        if _then_compact(node):
            lines = _inline_leg(node['body'][0], level, f"If {cond} Then ")
            then_is_block = False
        else:
            lines = [f"{pad}If {cond} Then Begin"]
            lines += _emit_block(node['body'], level + 1)
            then_is_block = True
        # ELSE leg.
        if eb is not None:
            # A Begin/End then leg closes with `End` (no `;`) so the Else binds here;
            # a compact then leg already carries its own terminating `;`.
            if then_is_block:
                lines.append(f"{pad}End")
            if _else_compact(node):
                lines += _inline_leg(eb[0], level, "Else ")
            else:
                lines.append(f"{pad}Else Begin")
                lines += _emit_block(eb, level + 1)
                lines.append(f"{pad}End ;")
        elif then_is_block:
            lines.append(f"{pad}End ;")
        return "\n".join(lines)

    if t == 'for':
        direction = "downto" if node.get('downto') else "to"
        var = node.get('_var_ident') or node['var']
        head = (f"For {var} = {emit_expr(node['start'])} "
                f"{direction} {emit_expr(node['end'])} ")
        if _loop_compact(node):
            lines = _inline_leg(node['body'][0], level, head)
        else:
            lines = [f"{pad}{head}Begin"]
            lines += _emit_block(node['body'], level + 1)
            lines.append(f"{pad}End ;")
        return "\n".join(lines)

    if t == 'while':
        head = f"While {emit_expr(node['cond'])} "
        if _loop_compact(node):
            lines = _inline_leg(node['body'][0], level, head)
        else:
            lines = [f"{pad}{head}Begin"]
            lines += _emit_block(node['body'], level + 1)
            lines.append(f"{pad}End ;")
        return "\n".join(lines)

    if t == 'repeat_until':
        lines = [f"{pad}Repeat"]
        lines += _emit_block(node['body'], level + 1)
        lines.append(f"{pad}Until {emit_expr(node['cond'])} ;")
        return "\n".join(lines)

    if t == 'once':
        if _loop_compact(node):
            lines = _inline_leg(node['body'][0], level, "Once ")
        else:
            lines = [f"{pad}Once Begin"]
            lines += _emit_block(node['body'], level + 1)
            lines.append(f"{pad}End ;")
        return "\n".join(lines)

    if t == 'switch':
        lines = [f"{pad}Switch {emit_expr(node['expr'])} Begin"]
        cpad = _ind(level + 1)
        for case in node['cases']:
            lines.append(f"{cpad}Case {emit_expr(case['value'])}:")
            lines += _emit_block(case['body'], level + 2)
        if node.get('default') is not None:
            lines.append(f"{cpad}Default:")
            lines += _emit_block(node['default'], level + 2)
        lines.append(f"{pad}End ;")
        return "\n".join(lines)

    if t == 'attribute':
        name = _canon(node['name'])
        if node.get('value') is not None:
            body = f"{name} = {emit_expr(node['value'])}"
        elif node.get('raw_args') is not None:
            body = f"{name}({node['raw_args']})"
        else:
            body = name
        return f"{pad}[{body}]"

    if t == 'break':
        return f"{pad}Break ;"
    if t == 'continue':
        return f"{pad}Continue ;"
    if t == 'abort':
        return f"{pad}Abort ;"

    if t == 'order':
        return f"{pad}{_emit_order(node)}"

    if t == 'risk':
        return f"{pad}{_emit_risk(node)}"

    if t == 'plot':
        return f"{pad}{_emit_plot(node)}"

    if t == 'alert':
        return f"{pad}Alert( {emit_expr(node['msg'])} ) ;"

    if t == 'commentary':
        return f"{pad}Commentary( {emit_expr(node['msg'])} ) ;"

    if t == 'print':
        inner = ", ".join(emit_expr(a) for a in node['args'])
        return f"{pad}Print( {inner} ) ;"

    raise ELEmitError(f"cannot emit statement node of type {t!r}: {node!r}{_eln(node)}")


def _emit_order(node):
    action = _ORDER_ACTION.get(node['action'])
    if action is None:
        raise ELEmitError(f"order with unknown action {node['action']!r}{_eln(node)}")
    out = [action]
    # label: '' is the parser's default (no label); anything else is an expr node.
    label = node.get('label')
    if label != '' and label is not None:
        out.append(f"( {emit_expr(label)} )")
    if node.get('quantity') is not None:
        qty_txt = emit_expr(node['quantity'])
        # A literal quantity of 1 emits the singular noun ('1 contract'); the
        # parser accepts singular 'contract'/'share' (parser.py:1131) so the
        # reparse-fixpoint holds. Non-literal or !=1 quantities stay plural.
        noun = "contract" if qty_txt.strip() == '1' else "contracts"
        out.append(f"{qty_txt} {noun}")
        if node.get('total'):
            out.append("total")
    if node.get('entry_label') is not None:
        out.append(f"from entry ( {emit_expr(node['entry_label'])} )")

    bar_timing = node.get('bar_timing', 'next')
    order_type = node.get('order_type', 'market')
    if bar_timing == 'this' and order_type == 'close':
        out.append("this bar on close")
    elif order_type == 'market':
        # `next bar at market` is the price-less market order; but the parser also
        # accepts `next bar at <price>` (a price with NO stop/limit keyword — e.g.
        # `next bar at open`), storing that price with order_type still 'market'.
        # Re-emit the price verbatim in that case so the fixpoint holds.
        if node.get('price') is not None:
            out.append(f"next bar at {emit_expr(node['price'])}")
        else:
            out.append("next bar at market")
    elif order_type in ('limit', 'stop'):
        if node.get('price') is None:
            raise ELEmitError(f"{order_type} order missing a price{_eln(node)}")
        out.append(f"next bar at {emit_expr(node['price'])} {order_type}")
    else:
        raise ELEmitError(
            f"order with unemittable timing/type {bar_timing!r}/{order_type!r}{_eln(node)}")
    return " ".join(out) + " ;"


def _emit_risk(node):
    name = _canon(node['func'])
    if 'args' in node:
        inner = ", ".join(emit_expr(a) for a in node['args'])
        return f"{name}( {inner} ) ;"
    if node.get('arg') is not None:
        return f"{name}( {emit_expr(node['arg'])} ) ;"
    return f"{name} ;"


def _emit_plot(node):
    name = _canon(node['name'])
    args = [emit_expr(node['value'])]
    label = node.get('label')
    if label is not None:
        if isinstance(label, str):
            args.append(_emit_string(label))
        else:
            args.append(emit_expr(label))
    for extra in node.get('extra_args', []):
        args.append(emit_expr(extra))
    return f"{name}( " + ", ".join(args) + " ) ;"


# --- public entrypoint -----------------------------------------------------
def emit_el(ast):
    """Emit compile-safe EL for a full program AST (the shared IR dict).

    Takes the AST ONLY — no source text. Raises ELEmitError on any node it
    cannot faithfully emit."""
    if not isinstance(ast, dict) or ast.get('type') != 'program':
        raise ELEmitError("emit_el expects a 'program' AST node")
    header, leading, trailing, footer = _anchor_comments(
        ast['body'], ast.get('_comments') or [])
    global _LEADING, _TRAILING
    _LEADING, _TRAILING = leading, trailing
    try:
        parts = []
        # Leading blank lines at the top of the source, reproduced verbatim from the
        # `_leading_blanks` count the EL parser recorded from raw source text. Blank
        # lines yield no tokens, so the reparse-fixpoint AST is unchanged; re-emitting
        # the same count keeps it a fixed point (Rcyc). Only the EL parser sets this
        # key, so the py_front two-way path emits no fabricated blanks.
        n_blank = ast.get('_leading_blanks')
        if isinstance(n_blank, int) and n_blank > 0:
            parts.extend([""] * n_blank)
        parts.extend(_comment_lines(header, ""))
        parts.extend(_emit_seq(ast['body'], 0))
        parts.extend(_comment_lines(footer, ""))
        body = "\n".join(parts)
    finally:
        _LEADING, _TRAILING = {}, {}
    return _wrap_all(body) + "\n"
