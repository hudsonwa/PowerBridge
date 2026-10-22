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

"""#BeginCmtry blocks and Commentary with several arguments.

PowerBridge has no commentary-on mode, so a #BeginCmtry … #End block is
parsed, kept in the AST for round-tripping, and not executed.
Commentary("a", "b") is one message joined with +; CommentaryCL is the same.
"""
import pytest

from pl_transpiler import transpile
from pl_transpiler.codegen_el import emit_el
from pl_transpiler.lexer import PLSyntaxError, tokenise
from pl_transpiler.parser import parse
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_oracle import run_oracle

ES = get_config("ES")

_BARS = [
    {"date": 1260105, "time": 1000 + i, "open": 10 + i, "high": 12 + i,
     "low": 9 + i, "close": 11 + i, "volume": 1}
    for i in range(5)
]

_BARE = 'If Time = 1000 Then Buy ("c001_LE") next bar at market;\n'
_BLOCK = (
    "Vars: N(0);\n"
    "#BeginCmtry\n"
    'Commentary("c001 TLs ", NumToStr(N, 0));\n'
    "#End;\n" + _BARE
)


def _ast(src):
    tokens, positions = tokenise(src)
    return parse(tokens, positions=positions, source=src)


def test_commentary_block_is_not_executed():
    assert "c001 TLs" not in transpile(_BLOCK)
    assert (run_oracle(_BLOCK, _BARS, ES)["sha256"]
            == run_oracle(_BARE, _BARS, ES)["sha256"])


def test_commentary_block_round_trips_through_el():
    emitted = emit_el(_ast(_BLOCK))
    assert "#BeginCmtry" in emitted and "#End" in emitted
    blocks = [n for n in _ast(emitted)["body"]
              if n["type"] == "commentary_block"]
    assert len(blocks) == 1
    msg = blocks[0]["body"][0]
    assert msg["type"] == "commentary"
    assert msg["msg"]["type"] == "binop" and msg["msg"]["op"] == "+"


def test_commentarycl_joins_arguments():
    node = _ast('CommentaryCL("a", "b");\n')["body"][0]
    assert node["cl"] is True and node["msg"]["op"] == "+"
    assert "_commentary" in transpile('CommentaryCL("a", "b");\n')


def test_unclosed_block_is_a_syntax_error():
    with pytest.raises(PLSyntaxError, match="#End"):
        transpile('#BeginCmtry\nCommentary("x");\n')
