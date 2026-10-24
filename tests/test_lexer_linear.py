"""The linear-time lexer (#180) gives exactly what the old quadratic one gave.

Every case is tokenised by pl_transpiler.lexer and by the frozen pre-#180 copy in
tests/_lexer_reference.py; tokens, positions, comments and error messages must
match. A timing guard keeps a 3k-line program well under a second.
"""
import glob
import os
import time

import pytest

from pl_transpiler import lexer
from tests import _lexer_reference as ref

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _corpus():
    paths = sorted(glob.glob(os.path.join(REPO, "ground_truth", "**", "*.txt"), recursive=True))
    paths += sorted(glob.glob(os.path.join(REPO, "examples", "*.el")))
    return paths


def _read(path):
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _program(n_lines):
    body = [
        "inputs: Len(14), Fast(5);",
        "variables: x(0), y(0.5), s(\"a\");",
        "{ block { nested } comment }",
    ]
    i = 0
    while len(body) < n_lines:
        body.append(f"x = Average(Close, Len) + {i}.25 * High[1]; // note {i}")
        body.append(f"if x crosses above y then Buy (\"L{i}\") next bar at market;")
        body.append(f"if Close crosses under x then begin y = y + 1; end; {{ c{i} }}")
        i += 1
    return "\n".join(body[:n_lines]) + "\n"


def _outcome(fn, src):
    try:
        res = fn(src)
    except lexer.PLSyntaxError as e:
        return ("error", str(e), e.line, e.col)
    return res


def _new(src):
    return lexer.tokenise_with_comments(src)


CASES = {
    "empty": "",
    "one_char": "x",
    "only_newlines": "\n\n\n",
    "crlf": "inputs: Len(14);\r\nvalue1 = Close; // c\r\n{ b }\r\nBuy next bar at market;\r\n",
    "nested_comments": "{ a { b { c } } d }x{}\n  { trailing } y // z",
    "comment_at_eof": "x = 1; // no newline",
    "token_at_eof": "x = 1;\ny",
    "crosses_over_newline": "if a crosses\n\n  over b then x = 1;",
    "crosses_alone": "x = crosses;",
    "unterminated_comment": "x = 1;\n  { open { inner }",
    "unterminated_string": "x = 1;\ny = \"abc\nz",
    "bad_char": "x = 1;\r\n  y = 2 @ 3;",
    "bad_char_first": "$",
    "program_3k": _program(3000),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_reference(name):
    src = CASES[name]
    assert _outcome(_new, src) == _outcome(ref.tokenise, src)


@pytest.mark.parametrize("path", _corpus(), ids=lambda p: os.path.relpath(p, REPO))
def test_corpus_matches_reference(path):
    src = _read(path)
    assert _outcome(_new, src) == _outcome(ref.tokenise, src)


def test_corpus_is_not_empty():
    assert len(_corpus()) > 10


def test_3k_lines_tokenise_fast():
    src = _program(3000)
    t0 = time.perf_counter()
    lexer.tokenise(src)
    assert time.perf_counter() - t0 < 1.0
