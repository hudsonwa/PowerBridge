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

"""reverse.py — the reverse transpiler's minimal public surface (GOAL R5).

Thin wrappers over the pieces proven by R2b/R4; NO framework, NO I/O beyond the
strings passed in. Each function is a one-liner composition of already-verified
components:

  emit_el(ast)        -> EL text        (re-export of codegen_el.emit_el)
  el_to_py(el_text)   -> clean Python   (the forward transpiler, trace=False)
  py_to_el(py_text)   -> EL text        (py_front -> emit_el)
  el_roundtrip(el)    -> EL text        (parse -> emit_el; the identity round-trip)

The public entrypoint for the loop/tools is this module + tools/el_emit.py.
"""
from pl_transpiler import transpile
from pl_transpiler.lexer import tokenise
from pl_transpiler.parser import parse
from pl_transpiler.codegen_el import emit_el as _emit_el
from pl_transpiler import py_front

# Re-export: the emitter is the AST-only R2b implementation, unchanged.
emit_el = _emit_el


def parse_el(el_text: str):
    """EL source text -> the parser's dict AST (the shared IR)."""
    tokens, positions = tokenise(el_text)
    return parse(tokens, positions=positions, source=el_text)


def el_to_py(el_text: str) -> str:
    """EL source text -> clean mirror-dialect Python (forward transpiler)."""
    return transpile(el_text, trace=False)


def py_to_el(py_text: str) -> str:
    """Clean mirror-dialect Python -> EL text (py_front inverse, then emit)."""
    return emit_el(py_front.py_to_ast(py_text))


def el_roundtrip(el_text: str) -> str:
    """EL -> AST -> EL (the AST-only identity round-trip; codegen_el.emit_el)."""
    return emit_el(parse_el(el_text))
