#!/usr/bin/env python3
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

"""
test_ast_retention.py — R1 parser-retention regression (reverse transpiler loop).

The reverse emitter needs surface facts the parser historically DISCARDED. This
test parses small EL snippets exercising each newly-retained field and asserts it
lands in the dict AST with the right value:

  1. IntraBarPersist on a Vars decl      -> decl['intrabar_persist'] is True
  2. IntraBarPersist on an Arrays decl    -> decl['intrabar_persist'] is True
  3. Plot extra args (color/style/width)  -> plot['extra_args'] retained, and the
     folded color/style constant keeps its original token name in '_symbol'
  4. [Name(args)] attribute arg list       -> attribute['raw_args'] == '<text>'
  5. order 'total' modifier (named exits)  -> order['total'] is True

Also asserts the NEGATIVE: a decl / plot / order WITHOUT the modifier does not
grow the new key (so forward output stays byte-identical).

Exit 0 only if every assertion holds.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler.lexer import tokenise
from pl_transpiler.parser import parse


def _parse(src):
    tokens, positions = tokenise(src)
    return parse(tokens, positions=positions, source=src)


def _find(ast, node_type):
    """Yield every node of the given 'type' anywhere in the AST."""
    if isinstance(ast, dict):
        if ast.get('type') == node_type:
            yield ast
        for v in ast.values():
            yield from _find(v, node_type)
    elif isinstance(ast, list):
        for item in ast:
            yield from _find(item, node_type)


_failures = []


def check(cond, msg):
    status = 'PASS' if cond else 'FAIL'
    print(f"  [{status}] {msg}")
    if not cond:
        _failures.append(msg)


# ---- 1. IntraBarPersist on a Vars decl ----
print("1. IntraBarPersist (Vars):")
ast = _parse("Vars: IntraBarPersist IbpCount( 0 ), Plain( 0 );")
vd = next(_find(ast, 'var_decl'))
ibp = next(d for d in vd['decls'] if d['name'] == 'ibpcount')
plain = next(d for d in vd['decls'] if d['name'] == 'plain')
check(ibp.get('intrabar_persist') is True, "IbpCount decl has intrabar_persist=True")
check('intrabar_persist' not in plain, "Plain decl does NOT grow intrabar_persist key")

# ---- 2. IntraBarPersist on an Arrays decl ----
print("2. IntraBarPersist (Arrays):")
ast = _parse("Arrays: IntraBarPersist Buf[10]( 0 );")
ad = next(_find(ast, 'array_decl'))
adecl = ad['decls'][0]
check(adecl.get('intrabar_persist') is True, "Buf array decl has intrabar_persist=True")

# ---- 3. Plot extra args + color _symbol retention ----
print("3. Plot extra args + color symbol:")
ast = _parse('Plot1( Close, "MyPlot", Red, Tool_Solid, 2 );')
plot = next(_find(ast, 'plot'))
extra = plot.get('extra_args')
check(isinstance(extra, list) and len(extra) == 3, "plot has extra_args of length 3")
check(extra[0].get('_symbol') == 'red',
      "1st extra arg (Red) keeps _symbol='red' (value still folded)")
check(extra[0].get('value') == str(0xFF0000),
      "Red still folds to its numeric value for codegen")
check(extra[1].get('_symbol') == 'tool_solid',
      "2nd extra arg (Tool_Solid) keeps _symbol='tool_solid'")
check(extra[2].get('type') == 'number' and extra[2].get('value') == '2'
      and '_symbol' not in extra[2],
      "3rd extra arg (plain width 2) is an unadorned number")
# a plot with no extra args must NOT grow the key
ast_ne = _parse('Plot2( Close, "Bare" );')
plot_ne = next(_find(ast_ne, 'plot'))
check('extra_args' not in plot_ne, "plot with no extra args does NOT grow extra_args key")

# ---- 4. [Name(args)] attribute raw_args ----
print("4. Attribute raw_args:")
ast = _parse("[Data( 2 )];")
attr = next(_find(ast, 'attribute'))
check(attr.get('raw_args') == '2', "[Data(2)] retains raw_args == '2'")
# the '=' value form must NOT grow raw_args
ast_eq = _parse("[IntrabarOrderGeneration = true];")
attr_eq = next(_find(ast_eq, 'attribute'))
check('raw_args' not in attr_eq, "[Name = Value] form does NOT grow raw_args key")

# ---- 5. order 'total' modifier ----
print("5. Order 'total' modifier:")
ast = _parse('Sell( "LX" ) 100 shares total next bar at market;')
order = next(_find(ast, 'order'))
check(order.get('total') is True, "Sell ... total retains total=True")
# an order without 'total' must NOT grow the key
ast_nt = _parse('Buy( "LE" ) 100 shares next bar at market;')
order_nt = next(_find(ast_nt, 'order'))
check('total' not in order_nt, "order without 'total' does NOT grow total key")

# ---- 6. R8: compact single-statement block flags ----
print("6. Begin/End block-flag retention (R8):")
# compact single statement -> NO block flag
ast = _parse("If c > 0 Then x = 1;")
iff = next(_find(ast, 'if'))
check('then_block' not in iff, "`If c Then x = 1` grows NO then_block flag (compact)")
# explicit Begin/End around a single statement -> flag True
ast_b = _parse("If c > 0 Then Begin x = 1; End;")
iff_b = next(_find(ast_b, 'if'))
check(iff_b.get('then_block') is True,
      "`If c Then Begin x = 1 End` retains then_block=True")
# else leg + for/while/once block flags
ast_e = _parse("If c > 0 Then Begin x = 1; End Else Begin y = 2; End;")
iff_e = next(_find(ast_e, 'if'))
check(iff_e.get('else_block') is True, "explicit Else Begin/End retains else_block=True")
ast_f = _parse("For i = 1 To 3 Begin x = 1; End;")
forn = next(_find(ast_f, 'for'))
check(forn.get('block') is True, "`For .. Begin .. End` retains block=True")
ast_fc = _parse("For i = 1 To 3 x = 1;")
forn_c = next(_find(ast_fc, 'for'))
check('block' not in forn_c, "compact `For .. <stmt>` grows NO block flag")

# ---- 7. R8: user-identifier original casing ----
print("7. User-identifier original casing (_ident, R8):")
ast = _parse("Vars: FastMA( 0 );\nFastMA = Close;")
vd = next(_find(ast, 'var_decl'))
d = vd['decls'][0]
check(d.get('_ident') == 'FastMA', "declared `FastMA` retains _ident='FastMA' (folded name lowercase)")
check(d['name'] == 'fastma', "declared name is folded to lowercase")
use = next(n for n in _find(ast, 'ident') if n.get('name') == 'fastma')
check(use.get('_ident') == 'FastMA', "used `FastMA` retains _ident='FastMA'")
# a built-in keyword must NOT grow _ident (only user identifiers carry casing)
ast_bi = _parse("x = Close;")
close_use = next((n for n in _find(ast_bi, 'ident') if n.get('name') == 'close'), None)
check(close_use is None or '_ident' not in close_use,
      "built-in `Close` does NOT grow _ident (builtins keep signature casing)")

print()
if __name__ == "__main__":
    if _failures:
        print(f"RETENTION FAILED: {len(_failures)} assertion(s) failed")
        sys.exit(1)
    print("RETENTION OK: all fields retained in the AST")
    sys.exit(0)
