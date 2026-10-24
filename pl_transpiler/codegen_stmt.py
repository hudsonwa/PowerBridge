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

"""Statement generation for CodeGen (mixin; see codegen.py)."""

from typing import TYPE_CHECKING, Any, Optional

from . import catalog as _catalog
from ._types import Node
from .codegen_util import (
    _expr_already_f32,
    _f32_literal_const,
    _is_numeric_literal_node,
    call_arity_problem,
)

# EasyLanguage strategy-performance reserved words missing from the keyword
# catalog. Assigning to one is an unimplemented-keyword error, not an
# undeclared variable (see gen_assign).
_EL_RESERVED_NOT_IN_CATALOG = frozenset((
    'avgwintrade', 'avglostrade', 'avgtrade', 'grossprofit', 'grossloss',
    'netprofit', 'numwintrades', 'numlostrades', 'numeventrades', 'totaltrades',
    'percentprofit', 'largestwintrade', 'largestlostrade', 'maxconsecwinners',
    'maxconseclosers', 'totalbarswintrades', 'totalbarslostrades',
    'totalbarseventrades', 'entryprice', 'currentcontracts', 'tradestoday',
))


class _StmtMixin:
    if TYPE_CHECKING:
        # Host interface provided by CodeGen / _ExprMixin (see codegen.py).
        lines: list[str]
        indent: int
        has_once: bool
        trace: bool
        persist: bool
        _series_context: bool
        _partial: bool
        _need_f32_import: bool
        _cur_stmt_line: Optional[int]
        _emit_line: Optional[int]
        _trace_vars: set[str]
        _stateful_vars: set[str]
        _numeric_vars: set[str]
        _double_accum_vars: set[str]
        _history_vars: set[str]
        _array_vars: set[str]
        _hoisted_input_names: set[str]
        _cmt_lead: dict[int, list[str]]
        _cmt_trail: dict[int, list[str]]
        _unimpl_errors: list[dict[str, Any]]

        def emit(self, line: str) -> None: ...
        def _el_name(self, name: str) -> str: ...
        def gen_expr(self, node: Node) -> str: ...
        def _is_known_bare_ident(self, name: str) -> bool: ...

    def _emit_comment(self, text: str) -> None:
        """Emit a verbatim EL comment as `#| ` line(s) (clean mode only). A
        multi-line `{...}` comment keeps its internal newlines, one `#|` line each."""
        for phys in text.split('\n'):
            self.emit('#| ' + phys)

    def gen_stmt(self, node: Node) -> None:
        for txt in self._cmt_lead.get(id(node), ()):
            self._emit_comment(txt)
        start = len(self.lines)          # first physical line this statement emits
        if node.get('_line'):
            self._cur_stmt_line = node['_line']
        outer_line = self._emit_line
        self._emit_line = node.get('_line') or outer_line
        ntype = node['type']
        method = getattr(self, f'gen_{ntype}', None)
        if method:
            method(node)
        else:
            raise ValueError(f"Unknown statement type: {ntype}")
        self._emit_line = outer_line
        # Trailing comment (anchored to this statement's own source line): emit it as a
        # same-line `#|` suffix on the statement's FIRST emitted line (which corresponds
        # to the EL statement's start line), so py_front recovers its trailing-ness and
        # re-anchors it to THIS statement — not the next one. Single-`#|`-line texts only
        # (a trailing anchor is always a one-line `{...}`); a multi-physical-line block
        # comment is never trailing. Falls back to an own-line comment if the statement
        # somehow emitted nothing.
        trail = self._cmt_trail.get(id(node))
        if trail:
            if len(self.lines) > start and all('\n' not in t for t in trail):
                self.lines[start] += '  ' + ' '.join('#| ' + t for t in trail)
            else:
                for txt in trail:
                    self._emit_comment(txt)

    def gen_attribute(self, node: Node) -> None:
        # Strategy/study attribute (e.g. [SameExitFromOneEntryOnce = True]) — a
        # declaration with no effect on the per-bar dumped column values, so it
        # emits nothing. Recorded as a comment for traceability.
        self.emit(f"# attribute [{node.get('name')}] (no-op for calc/trace)")

    # ---- Variable Declarations ----
    def gen_var_decl(self, node: Node) -> None:
        for d in node['decls']:
            name = d['name']
            init_val = self.gen_expr(d['init'])
            if name in self._array_vars:
                # Arrays handled by gen_array_decl
                self.emit(f"{name} = _state.get('{name}', [{init_val}] * 1)")
            elif self.persist and name in self._stateful_vars:
                # Previous bar's end value. Unindexed vars were unpacked from
                # `_pb_slots` in the prologue. Lookback vars load the last
                # appended value. Numeric non-accumulators were f32-narrowed at
                # seed and on every assignment, so the load does not re-wrap
                # (idempotent, but 20 ctypes calls/bar on the reference strategy).
                # Self-referential accumulators stay double (see _collect_double_accums).
                if name in self._history_vars:
                    self.emit(f"{name} = _state['{name}'][-1]")
                # else: already bound from `_pb_slots` unpack above.
            else:
                # Clean (non-persist) dialect: use the init value each bar. This is
                # why transpile(trace=False) cannot run a stateful backtest.
                if name in self._numeric_vars:
                    self.emit(f"{name} = f32({init_val})")
                    self._need_f32_import = True
                else:
                    self.emit(f"{name} = {init_val}")

    # ---- Input Declarations ----
    def gen_input_decl(self, node: Node) -> None:
        for d in node['decls']:
            name = d['name']
            if name in self._hoisted_input_names:
                # Persist/trace: Inputs were bound from `_state['__pb_inputs']`
                # in the first-bar prologue. Trace still snapshots the value.
                if self.trace and name in self._trace_vars:
                    self.emit(f"_trace[{self._el_name(name)!r}] = {name}")
                continue
            default_val = self.gen_expr(d['default'])
            val = f"kwargs.get({self._el_name(name)!r}, {default_val})"
            self.emit(f"{name} = {val}")
            if self.trace and name in self._trace_vars:
                self.emit(f"_trace[{self._el_name(name)!r}] = {name}")

    def _gen_block(self, stmts: list[Node]) -> None:
        """Emit an indented statement body, or `pass` when it produced no code.
        A body can be non-empty yet emit nothing (e.g. only a redeclaration of a
        top-level variable), and comment lines alone are not a Python block."""
        start = len(self.lines)
        for s in stmts:
            self.gen_stmt(s)
        if not any(not ln.lstrip().startswith('#') for ln in self.lines[start:]):
            self.emit('pass')

    # ---- If Statement ----
    def gen_if(self, node: Node) -> None:
        cond = self.gen_expr(node['cond'])
        self.emit(f"if {cond}:")
        self.indent += 1
        self._gen_block(node['body'])
        self.indent -= 1

        if node.get('else_body'):
            self.emit('else:')
            self.indent += 1
            self._gen_block(node['else_body'])
            self.indent -= 1

    # ---- For Loop ----
    def gen_for(self, node: Node) -> None:
        var = node['var']
        start = self.gen_expr(node['start'])
        end = self.gen_expr(node['end'])
        # Wrap in int() because EL for-loop bounds may be float32 (e.g. f32(len))
        if node['downto']:
            self.emit(f"for {var} in range(int({start}), int({end}) - 1, -1):")
        else:
            self.emit(f"for {var} in range(int({start}), int({end}) + 1):")
        self.indent += 1
        self._gen_block(node['body'])
        self.indent -= 1

    # ---- While Loop ----
    def gen_while(self, node: Node) -> None:
        cond = self.gen_expr(node['cond'])
        self.emit(f"while {cond}:")
        self.indent += 1
        self._gen_block(node['body'])
        self.indent -= 1

    # ---- Repeat..Until ----
    def gen_repeat_until(self, node: Node) -> None:
        cond = self.gen_expr(node['cond'])
        self.emit("while True:")
        self.indent += 1
        if node['body']:
            for s in node['body']:
                self.gen_stmt(s)
        self.emit(f"if {cond}:")
        self.indent += 1
        self.emit("break")
        self.indent -= 1
        self.indent -= 1

    # ---- Once ----
    def gen_once(self, node: Node) -> None:
        self.has_once = True
        self.emit("if _first_bar:")
        self.indent += 1
        for s in node['body']:
            self.gen_stmt(s)
        self.emit("_first_bar = False")
        self.indent -= 1

    # ---- Switch/Case ----
    def gen_switch(self, node: Node) -> None:
        expr = self.gen_expr(node['expr'])
        # FAIL-CLOSED: the switch subject is otherwise emitted ONLY inside the
        # per-case `if/elif <expr> == <val>:` lines. With ZERO case labels
        # (`Switch (X) Begin End;` or a Default-only switch) that loop emits
        # nothing, so the subject expression — and, in partial mode, any
        # pl_partial_stub(...)() it carries — would be silently dropped (the run
        # would complete with a wrong value while the watermark claims a stub) and a
        # Default-only switch would emit an orphan `else:` (a SyntaxError). EL always
        # evaluates the subject once regardless of case matches, so with no case
        # labels emit the subject as a standalone statement (evaluating it — and
        # firing any stub) and, if present, run the Default body unconditionally
        # (nothing can match). Switches with >=1 case take the normal path below.
        if not node['cases']:
            self.emit(expr)
            if node.get('default'):
                for s in node['default']:
                    self.gen_stmt(s)
            return
        for i, case in enumerate(node['cases']):
            val = self.gen_expr(case['value'])
            keyword = 'if' if i == 0 else 'elif'
            self.emit(f"{keyword} {expr} == {val}:")
            self.indent += 1
            self._gen_block(case['body'])
            self.indent -= 1
        if node.get('default'):
            self.emit('else:')
            self.indent += 1
            self._gen_block(node['default'])
            self.indent -= 1

    # ---- Array Declaration ----
    def gen_array_decl(self, node: Node) -> None:
        for d in node['decls']:
            name = d['name']
            init = self.gen_expr(d['init'])
            if d['size'] is None:
                # Dynamic array: starts as a single-element list, grown by pl_array_setmaxindex
                default = f"[{init}]"
            else:
                size = self.gen_expr(d['size'])
                default = f"[{init}] * ({size} + 1)"
            if self.persist:
                # Load array from state (persistent between bars); when absent,
                # initialize the SAME sized list the non-persist path builds.
                self.emit(f"{name} = _state.get('{name}', {default})")
            else:
                self.emit(f"{name} = {default}")
            if self.trace and name in self._trace_vars:
                self.emit(f"_trace[{self._el_name(name)!r}] = {name}")

    # ---- Commentary ----
    def gen_commentary(self, node: Node) -> None:
        extra = node.get('extra_args') or []
        if not extra:
            self.emit(f"_commentary = {self.gen_expr(node['msg'])}")
            return
        # Each argument becomes text with str() before joining. No
        # PowerBridge test checks this against MultiCharts' own number
        # formatting; the native kernel evaluates and drops the message.
        parts = ', '.join(self.gen_expr(a) for a in [node['msg'], *extra])
        self.emit(f"_commentary = ''.join(str(_a) for _a in ({parts},))")

    def gen_commentary_block(self, node: Node) -> None:
        # #BeginCmtry … #End is not executed. PowerBridge has no
        # commentary-on mode. The statements stay in the AST so
        # el_emit can round-trip them; they do not affect trades or drawings.
        if not isinstance(node.get("body"), list):
            raise ValueError("commentary_block body must be a list")

    # ---- Break / Continue ----
    def gen_break(self, node: Node) -> None:
        self.emit('break')

    def gen_continue(self, node: Node) -> None:
        self.emit('continue')

    # ---- Abort ----
    def gen_abort(self, node: Node) -> None:
        # EL Abort halts the run-time calculation. In transpiled traces it is
        # only reached in never-true compile-coverage branches, so emit a no-op.
        self.emit('pass  # Abort (no-op for calc/trace)')

    # ---- Assignment ----
    def gen_assign(self, node: Node) -> None:
        before = len(self._unimpl_errors)
        target = self.gen_expr(node['target'])
        # A plain identifier on the LEFT of `=` that is neither declared nor an EL
        # keyword is an undeclared variable, not a missing builtin: tag it so the
        # diagnostic says so.
        tgt = node['target']
        name = tgt.get('name')
        if (tgt.get('type') == 'ident' and len(self._unimpl_errors) > before
                and self._unimpl_errors[before]['name'] == name
                and name not in _EL_RESERVED_NOT_IN_CATALOG
                and _catalog.get(name) is None
                and not self._is_known_bare_ident(name)):
            self._unimpl_errors[before]['kind'] = 'undeclared'
        # Partial mode: if the assignment TARGET is itself an unimplemented keyword,
        # gen_expr returned a stub call `pl_partial_stub('<name>', <line>)()` (partial
        # mode only — strict records+raises at generate end and never reaches here).
        # Emitting `pl_partial_stub(...)() = rhs` is a Python SyntaxError. Emit the
        # stub as a STANDALONE statement instead, so execution raises
        # UnimplementedKeywordError at this line and the file still compiles. The RHS
        # is still WALKED (for watermark completeness) but its emitted text is dropped.
        if (self._partial and isinstance(target, str)
                and target.startswith('pl_partial_stub(')):
            self.gen_expr(node['value'])
            self.emit(target)
            return
        # Handle multi-output Stochastic: returns (status, FastK, FastD, SlowK, SlowD)
        if node['value']['type'] == 'call' and node['value']['name'] == 'stochastic':
            self._gen_stochastic_assign(target, node['value'])
            return
        value_node = node['value']
        # Wrap with f32 for numeric user variables, EXCEPT self-referential
        # accumulators (RunSum = RunSum + Close), which EL keeps in double precision.
        # The pl_* runtime fns self-round their results and data series are f32 at
        # access, so dropping the wrap here keeps an unbounded accumulator in double
        # (matching the GT1 runsum capture) without affecting function/data values.
        wrap_f32 = (
            node['target']['type'] == 'ident'
            and node['target']['name'] in self._numeric_vars
            and node['target']['name'] not in self._double_accum_vars
        )
        # persist/trace: fold f32(<numeric literal>) to a bit-identical constant
        # so the two `X = 0` resets are not a ctypes round-trip every bar
        # Clean dialect keeps `f32(<lit>)` — py_front already
        # strips that wrapper, and DIALECT_VERSION stays 3.
        if wrap_f32 and self.persist and _is_numeric_literal_node(value_node):
            value = _f32_literal_const(value_node)
        else:
            value = self.gen_expr(value_node)
            if wrap_f32:
                # Persist/trace: skip a second wrap when the RHS is already f32
                # (hoisted `_close`, or a whole `f32(...)` call). Clean dialect
                # keeps the historical `f32(f32(close[-1]))` so py_front is stable.
                if not ((self.persist or self.trace) and _expr_already_f32(value)):
                    value = f"f32({value})"
                self._need_f32_import = True
        self.emit(f"{target} = {value}")
        # Trace user-variable assignments
        if self.trace and node['target']['type'] == 'ident':
            vname = node['target']['name']
            if vname in self._trace_vars:
                self.emit(f"_trace[{self._el_name(vname)!r}] = {vname}")

    def _gen_stochastic_assign(self, target: str, call_node: Node) -> None:
        """Generate multi-statement unpacking for Stochastic's (status, FastK, FastD, SlowK, SlowD) tuple."""
        # Args: high, low, close, length, smoothk, smoothd, smoothtype, oFastK, oFastD, oSlowK, oSlowD
        if call_arity_problem('stochastic', len(call_node['args'])) is not None:
            self.emit(f"{target} = {self.gen_expr(call_node)}")
            return
        input_args = call_node['args'][:7]  # first 7 are inputs
        output_names = [a['name'] for a in call_node['args'][7:]]  # last 4 are output var names

        # Process input args with series context
        old_ctx = self._series_context
        self._series_context = True
        arg_exprs = [self.gen_expr(a) for a in input_args]
        self._series_context = old_ctx

        call_str = f"pl_stochastic({', '.join(arg_exprs)})"
        result_var = "_stoch_result"

        self.emit(f"{result_var} = {call_str}")
        # Unpack: [0]=status->target, [1]=FastK, [2]=FastD, [3]=SlowK, [4]=SlowD
        # Target (stochret) gets status at [0]
        if target in self._numeric_vars:
            self.emit(f"{target} = f32({result_var}[0])")
            self._need_f32_import = True
        else:
            self.emit(f"{target} = {result_var}[0]")
        if self.trace and target in self._trace_vars:
            self.emit(f"_trace[{self._el_name(target)!r}] = {target}")
        # Output vars get elements [1]..[4]
        for i, name in enumerate(output_names):
            if name in self._numeric_vars:
                self.emit(f"{name} = f32({result_var}[{i + 1}])")
                self._need_f32_import = True
            else:
                self.emit(f"{name} = {result_var}[{i + 1}]")
            if self.trace and name in self._trace_vars:
                self.emit(f"_trace[{self._el_name(name)!r}] = {name}")

    # ---- Expression Statement ----
    def gen_expr_stmt(self, node: Node) -> None:
        self.emit(self.gen_expr(node['expr']))

    # ---- Order ----
    def gen_order(self, node: Node) -> None:
        action = node['action']
        otype = node['order_type']
        label = node.get('label', '')
        # label can be a string (old-style literal) or an AST dict (expression)
        if isinstance(label, dict):
            label_expr = self.gen_expr(label)
        else:
            label_expr = repr(label)
        qty = node.get('quantity')
        if qty and isinstance(qty, dict) and qty.get('name') == 'all':
            # EL 'all' = entire position: emit current_contracts from runtime
            qty_expr = '_current_contracts'
        elif qty:
            qty_expr = self.gen_expr(qty)
        else:
            qty_expr = '0'
        # Optional "from entry("label")" reference (mode 4 named exits)
        entry_label = node.get('entry_label')
        if entry_label is not None:
            entry_label_expr = self.gen_expr(entry_label) if isinstance(entry_label, dict) else repr(entry_label)
        else:
            entry_label_expr = None

        timing = node.get('bar_timing', 'next')
        # The `total` order modifier (EL: `... N contracts total ...`) is a rare,
        # position-sizing-scope flag. It is appended as a trailing sentinel string
        # 'total' ONLY when present, so non-total orders keep the plain 5/6-tuple
        # (every GT source) and the FillEngine's type-based price detection at
        # order[5] is unaffected. py_front._inv_order strips it back to node['total'].
        total_suffix = ", 'total'" if node.get('total') else ""
        if node.get('price'):
            price_str = self.gen_expr(node['price'])
            if entry_label_expr:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}, {price_str}, {entry_label_expr}{total_suffix}))")
            else:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}, {price_str}{total_suffix}))")
        else:
            if entry_label_expr:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}, {entry_label_expr}{total_suffix}))")
            else:
                self.emit(f"_orders.append(('{action}', '{otype}', {qty_expr}, '{timing}', {label_expr}{total_suffix}))")

    # ---- Risk ----
    def gen_risk(self, node: Node) -> None:
        func = node['func']
        if func == 'setstoploss':
            self.emit(f"_risk['stop_loss'] = {self.gen_expr(node['arg'])}")
        elif func == 'setprofittarget':
            self.emit(f"_risk['profit_target'] = {self.gen_expr(node['arg'])}")
        elif func == 'setexitonclose':
            self.emit("_risk['exit_on_close'] = True")
        elif func == 'setdollartrailing':
            self.emit(f"_risk['dollar_trailing'] = {self.gen_expr(node['arg'])}")
        elif func == 'setpercenttrailing':
            args = node.get('args', [node.get('arg')])
            profit = self.gen_expr(args[0])
            pct = self.gen_expr(args[1]) if len(args) > 1 else '0'
            self.emit(f"_risk['percent_trailing'] = ({profit}, {pct})")
        elif func == 'setstopposition':
            if node.get('arg') is not None:
                self.emit(f"_risk['stop_position'] = {self.gen_expr(node['arg'])}")
            else:
                self.emit("_risk['stop_position'] = True")
        elif func == 'setstopcontract':
            self.emit("_risk['stop_contract'] = True")
        elif func == 'setstopshare':
            self.emit("_risk['stop_share'] = True")
        elif func == 'setbreakeven':
            self.emit(f"_risk['breakeven'] = {self.gen_expr(node['arg'])}")
        # MultiCharts point-based variants: the amount is in price points; the
        # engine converts it to currency (points * BigPointValue) when it folds
        # the risk update, so it then behaves exactly like the dollar form.
        elif func == 'setstoploss_pt':
            self.emit(f"_risk['stop_loss_pt'] = {self.gen_expr(node['arg'])}")
        elif func == 'setprofittarget_pt':
            self.emit(f"_risk['profit_target_pt'] = {self.gen_expr(node['arg'])}")
        elif func == 'setbreakeven_pt':
            self.emit(f"_risk['breakeven_pt'] = {self.gen_expr(node['arg'])}")
        elif func == 'setpercenttrailing_pt':
            args = node['args']
            self.emit(f"_risk['percent_trailing_pt'] = ({self.gen_expr(args[0])}, {self.gen_expr(args[1])})")
        elif func == 'settrailingstop_pt':
            self.emit(f"_risk['trailing_stop_pt'] = {self.gen_expr(node['arg'])}")

    # ---- Plot ----
    def gen_plot(self, node: Node) -> None:
        name = node['name']
        value = self.gen_expr(node['value'])
        self.emit(f"_plots.setdefault('{name}', []).append({value})")
        if node.get('label'):
            label = node['label']
            if isinstance(label, dict):
                # Label is an AST expression node (variable, etc.)
                self.emit(f"_plots['{name}_label'] = {self.gen_expr(label)}")
            else:
                # Label is a plain string
                self.emit(f"_plots['{name}_label'] = '{label}'")

    # ---- Alert ----
    def gen_alert(self, node: Node) -> None:
        self.emit(f"_alerts.append({self.gen_expr(node['msg'])})")

    # ---- Print ----
    def gen_print(self, node: Node) -> None:
        args = ', '.join(self.gen_expr(a) for a in node['args'])
        self.emit(f"pl_print({args})")
