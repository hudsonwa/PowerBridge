"""One lex and one parse per distinct source per pl_run / pl_oracle run.

A run reaches the front end from the lookback scans, the native serialiser,
transpile and the strict pre-pass of --partial. ``parser.parse_source`` shares
one lex and one parse between them; these tests count the real calls.
"""

import os

import pytest

from pl_transpiler import lexer, parser
from pl_transpiler.lexer import PLSyntaxError
from pl_transpiler.runtime.accel import accel_available
from pl_transpiler.tools import pl_oracle, pl_run

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EL = os.path.join(REPO, "examples", "ma_cross.el")
BARS = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
UNIMPL_EL = "Vars: X(0);\nX = TradesToday(0);\nIf X > 5 then Buy next bar at market;\n"
NEEDS_NATIVE = pytest.mark.skipif(not accel_available(),
                                  reason="native kernel not built")


@pytest.fixture
def counts(monkeypatch):
    """Count real tokenise() and Parser.parse_program() calls, from a cold cache."""
    n = {"tokenise": 0, "parse": 0}
    real_tokenise = lexer.tokenise
    real_parse_program = parser.Parser.parse_program

    def tokenise(source):
        n["tokenise"] += 1
        return real_tokenise(source)

    def parse_program(self):
        n["parse"] += 1
        return real_parse_program(self)

    monkeypatch.setattr(lexer, "tokenise", tokenise)
    monkeypatch.setattr(parser, "tokenise", tokenise)
    monkeypatch.setattr(parser.Parser, "parse_program", parse_program)
    parser.clear_source_cache()
    yield n
    parser.clear_source_cache()


def _unimpl(tmp_path):
    path = tmp_path / "unimpl.el"
    path.write_text(UNIMPL_EL)
    return str(path)


@pytest.mark.parametrize("argv", [
    ["--python"],
    ["--python", "--max-bars-back", "0"],
    ["--python", "--max-bars-back", "auto"],
    pytest.param(["--native"], marks=NEEDS_NATIVE),
    pytest.param(["--native", "--max-bars-back", "auto"], marks=NEEDS_NATIVE),
])
def test_pl_run_lexes_and_parses_once(counts, capsys, argv):
    assert pl_run.main([*argv, EL, BARS]) == 0
    assert counts == {"tokenise": 1, "parse": 1}


def test_pl_run_partial_lexes_and_parses_once(counts, capsys, tmp_path):
    # --partial transpiles twice (strict pre-pass for the manifest, then the
    # partial build); both share one parse.
    pl_run.main(["--python", "--partial", _unimpl(tmp_path), BARS])
    assert "unimplemented" in capsys.readouterr().err.lower()
    assert counts == {"tokenise": 1, "parse": 1}


@pytest.mark.parametrize("argv", [
    ["--python"],
    pytest.param(["--native"], marks=NEEDS_NATIVE),
])
def test_pl_oracle_lexes_and_parses_once(counts, capsys, argv):
    assert pl_oracle.main([*argv, EL, BARS]) == 0
    assert counts == {"tokenise": 1, "parse": 1}


def test_parse_source_matches_parse():
    source = open(EL).read()
    tokens, positions = lexer.tokenise(source)
    assert parser.parse_source(source) == parser.parse(
        tokens, positions=positions, source=source)


def test_parse_source_returns_private_copies():
    source = open(EL).read()
    first = parser.parse_source(source)
    first["body"].clear()
    first["_comments"].clear()
    second = parser.parse_source(source)
    assert second["body"] and second["_comments"]
    assert second is not first


def test_parse_source_leaves_lexer_comments_as_tokenise_would():
    a = "Vars: X(0);\n// first\nX = 1;\n"
    b = "Vars: Y(0);\n{ second }\nY = 2;\n"
    parser.parse_source(a)
    parser.parse_source(b)
    parser.parse_source(a)  # cache hit
    lexer.tokenise(a)
    expected = [dict(c) for c in lexer.last_comments]
    parser.parse_source(b)
    parser.parse_source(a)
    assert lexer.last_comments == expected


def test_parse_errors_are_not_cached(counts):
    source = "if close > then buy next bar at market;\n"
    messages = []
    for _ in range(2):
        with pytest.raises(PLSyntaxError) as exc:
            parser.parse_source(source)
        messages.append(str(exc.value))
    assert messages[0] == messages[1]
    assert counts["parse"] == 2


def test_source_cache_is_bounded(counts):
    sources = [f"Vars: X(0);\nX = {i};\n" for i in range(parser._SOURCE_CACHE_SIZE + 3)]
    for source in sources:
        parser.parse_source(source)
    assert len(parser._source_cache) == parser._SOURCE_CACHE_SIZE
    parser.parse_source(sources[0])  # evicted: lexed and parsed again
    assert counts == {"tokenise": len(sources) + 1, "parse": len(sources) + 1}
