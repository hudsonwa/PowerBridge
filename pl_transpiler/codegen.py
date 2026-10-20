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

"""PowerLanguage Code Generator — walks AST and emits Python source."""


# Re-exported so existing `from pl_transpiler.codegen import UnimplementedKeywordError`
# imports keep working; the class itself now lives in pl_transpiler.errors.
from .errors import UnimplementedKeywordError  # noqa: F401

# Tables, helpers and the statement/expression mixins live in sibling modules;
# they are re-exported here so `from pl_transpiler.codegen import ...` keeps working.
from .codegen_tables import (  # noqa: F401
    DIALECT_VERSION,
    BUILTIN_FUNC_MAP,
    COMPARE_OPS,
    _SERIES_PARAMS,
)
from .codegen_util import (  # noqa: F401
    _available_pl_targets,
    _runtime_bound_bare_names,
    _OHLCV_F32_NAMES,
    _OHLCV_F32_LOCALS,
    _expr_already_f32,
    _is_numeric_literal_node,
    _f32_literal_const,
)
from .codegen_expr import _ExprMixin
from .codegen_stmt import _StmtMixin


class CodeGen(_ExprMixin, _StmtMixin):
    def __init__(self, trace=False, partial=False, persist=None):
        self.lines = []
        self.indent = 1  # start inside function body
        self.has_once = False
        self.has_math = False
        self._series_context = False  # True when inside function call args
        self.trace = bool(trace)
        # persist defaults to matching trace so existing callers are unchanged:
        #   trace=True  -> persist=True  (current trace dialect: _state + _trace)
        #   trace=False -> persist=False (clean mirror dialect for py_front)
        # persist=True with trace=False is the runnable no-trace dialect:
        # cross-bar `_state` without the per-bar `_trace[...]` stores.
        self.persist = bool(trace) if persist is None else bool(persist)
        self._trace_vars = set()  # user-declared variable names to trace
        self._stateful_vars = set()  # vars needing cross-bar persistence (var_decl only)
        self._numeric_vars = set()  # user-declared numeric (not string) vars
        self._double_accum_vars = set()  # self-referential numeric accumulators (kept double, not f32)
        self._history_vars = set()  # stateful vars that need a lookback list (Var[n] / series-fn arg)
        self._series_state = False  # runnable output seeds `_state` as a SeriesState
        self._slot_vars = []  # sorted unindexed persist vars packed into `_state['__pb_slots']`
        self._array_vars = set()  # user-declared array variable names
        self._need_f32_import = False  # set True when f32/el_round emitted
        self._state_inits = {}  # name -> string initial value for first-bar state seed
        self._input_inits = []  # (name, default_ast) for top-level Inputs (persist hoist)
        self._hoisted_input_names = set()  # names bound from _state['__pb_inputs']
        self._data_refs = {}  # name -> data_ref AST node (Data2-tied vars)
        self._cmt_lead = {}   # id(stmt) -> [verbatim EL comment, ...] (clean mode)
        self._cmt_trail = {}  # id(stmt) -> [verbatim EL comment, ...] (clean mode)
        self._cur_stmt_line = None  # nearest enclosing statement's EL line (for error messages)
        self._unimpl_errors = []  # collect-all: {'name','line'} for every unimplemented keyword
        self._partial = partial  # opt-in partial mode (unimplemented -> exec-time stubs)
        self._declared_all = set()  # EVERY var/input/array/for name at ANY depth (bare-keyword flagging)

    def emit(self, line):
        self.lines.append('    ' * self.indent + line)

    def gen_program(self, node):
        # Reset the collect-all diagnostics for this generate() (no cross-call leak).
        self._unimpl_errors = []
        # Collect EVERY declared name (Variables:/Inputs:/Arrays:/for-loop vars) at
        # EVERY nesting depth — Variables: blocks are legally declared INSIDE Begin/End
        # bodies (if/for/while/switch cases) mid-program, and round-tripped/emitted EL
        # re-positions them. The bare-keyword flag uses this set as the "is a real
        # declared identifier" oracle; a shallow (top-level-only) pass would wrongly
        # flag nested-declared vars as unimplemented. _collect_declared already walks
        # every nested body recursively, so it is the single source of truth here.
        from pl_transpiler.parser import _collect_declared
        self._declared_all = _collect_declared(node)
        # A bare ident that is ASSIGNED anywhere (at any depth) can be a legitimate
        # local — a plain `x = ...` statement DEFINES x for the emitted Python, exactly
        # as the forward path's `return name` always relied on. This matters for
        # round-tripped EL: py_front cannot tell an original `Variables:` declaration
        # from a plain assignment, so a nested-declared var comes back as a bare
        # `x = ...`; forward-transpiling that must NOT flag x's reads.
        #   Write-only guard: but this may ONLY whitelist targets whose value is also USED (read)
        # somewhere. A name that is ONLY EVER an assignment TARGET and never read, and
        # is not otherwise known (not Value*/Condition*/declared), is an assignment to a
        # read-only/unimplemented EL keyword (e.g. `AvgWinTrade = Close`, which EL
        # forbids) — treating it as a local silently BYPASSES the strict fail-loud.
        # Gating on "also read" keeps genuine `x = ...; y = x` locals valid while making
        # a write-only unimplemented keyword collect+raise (strict) / stub (partial).
        #   Read-before-write: "assigned & read" was still too broad. A recognized-but-
        # UNIMPLEMENTED EL keyword (e.g. FastD — a builtin marker with no faithful
        # bare expansion) or an unknown keyword that is read BEFORE it is ever
        # cleanly defined (`Value1 = FakeA; FakeA = 2;`) or is only ever self-
        # referential (`AvgWinTrade = AvgWinTrade + 1;`) is NOT a genuine local — it
        # has no defining write establishing a value, so rescuing it silently
        # bypasses the strict fail-loud (and drops it from the collect-all report).
        # _rescuable_assign_locals keeps ONLY names that (a) are not otherwise
        # recognized as a builtin/reserved keyword AND (b) have a clean defining
        # assignment (target not read on its own RHS) BEFORE their first read in
        # evaluation order — exactly the flattened nested-`Variables:` locals the
        # reverse path emits as bare `x = 0; ... = x`.
        _assigned = set()
        self._collect_assigned_targets(node, _assigned)
        _read = set()
        self._collect_read_names(node, _read)
        self._declared_all |= self._rescuable_assign_locals(node, _assigned & _read)
        # Collect user-declared var names first (before codegen body)
        for stmt in node['body']:
            if stmt['type'] == 'var_decl':
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    if name:
                        self._trace_vars.add(name)
                        self._stateful_vars.add(name)
                        # Check for Data2-tied initializer: Varname( expr, DataN )
                        if d.get('data_ref') is not None:
                            self._data_refs[name] = d['data_ref']
                            self._numeric_vars.add(name)
                            continue
                        # Determine if numeric: init is a number AST node
                        init = d.get('init', {})
                        if init.get('type') == 'number':
                            self._numeric_vars.add(name)
                            self._state_inits[name] = init['value']
                        elif init.get('type') == 'boolean':
                            self._state_inits[name] = '1' if init.get('value') else '0'
                        elif init.get('type') == 'string':
                            self._state_inits[name] = repr(init.get('value', ''))
                        else:
                            self._state_inits[name] = '0' if name in self._numeric_vars else "''"
            elif stmt['type'] == 'input_decl':
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    if name:
                        self._trace_vars.add(name)
                        default = d.get('default') or {'type': 'number', 'value': '0'}
                        self._input_inits.append((name, default))
                        if default.get('type') == 'number':
                            self._numeric_vars.add(name)
            elif stmt['type'] == 'array_decl':
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    if name:
                        self._trace_vars.add(name)
                        self._array_vars.add(name)

        # Detect self-referential numeric accumulators: assignments of the form
        # `Var = ...Var...` (Var referenced on its own RHS, as ident or Var[n]).
        # EL/PowerLanguage numeric variables are DOUBLE precision; an unbounded
        # accumulator that is f32-rounded every bar drifts at full range (proven by
        # GT1's runsum capture, which matches a plain float64 cumulative sum exactly).
        # We keep ONLY these self-referential numeric vars in double; every other
        # numeric var retains the existing f32 rounding (a blanket flip to double
        # regresses long-range trade parity, so we narrow per the capture).
        self._collect_double_accums(node['body'])
        # Unindexed Variables are stored as scalars. Only vars read as Var[n]
        # or passed into a series-context function keep a growing history list
        # (Audit Ref 3 has zero lookbacks — 30 appends/bar of unused history).
        self._history_vars = set()
        if self.persist:
            self._collect_history_vars(node['body'])

        # Partial mode: in partial persist/trace mode, a `Vars: x(<init containing an
        # unimplemented keyword>)` must make the RUN fail loud the moment x is
        # initialized — not silently start x at a literal default. Trace-mode
        # gen_var_decl reads each stateful var from _state and DISCARDS the
        # generated initializer, so the stub call that _require_pl_impl produces
        # for the unknown keyword would never reach the emitted body. Detect an
        # unimplemented keyword in each top-level var initializer here (walk it to
        # collect the same {name,line} the watermark reports) and, when found, make
        # the var's state SEED be the argless stub call, so the emitted
        # `_state['__pb_slots'] = [pl_partial_stub('x', <line>)()]` (or
        # `_state['x'] = [stub]` for a lookback list) raises
        # UnimplementedKeywordError on the first bar (inside the first-bar seed).
        # Strict (non-partial) mode is untouched — it raises at transpile time
        # via _raise_unimplemented().
        if self._partial and self.persist:
            for stmt in node['body']:
                if stmt.get('type') != 'var_decl':
                    continue
                for d in stmt.get('decls', []):
                    name = d.get('name')
                    init = d.get('init')
                    if (not name or not isinstance(init, dict)
                            or d.get('data_ref') is not None):
                        continue
                    before = len(self._unimpl_errors)
                    self.gen_expr(init)  # walk only — collect unimplemented keywords
                    if len(self._unimpl_errors) > before:
                        # Seed the var's state with an ARGLESS stub naming the first
                        # unimplemented keyword found in the initializer (accurate
                        # message, consistent with the watermark). A bare stub (not
                        # the full init expression) guarantees the run raises
                        # UnimplementedKeywordError — never some unrelated error from
                        # evaluating the rest of the initializer.
                        e = self._unimpl_errors[before]
                        self._state_inits[name] = (
                            f"pl_partial_stub({e['name']!r}, {e['line']!r})()")

        # Emit state initialization (persist/trace mode).
        # The clean mirror dialect (trace=False, persist=False) must NOT emit
        # `_state` — py_front reads that dialect back. persist=True is the
        # runnable no-trace path: same `_state` container, no `_trace` stores.
        #
        # Seed on the first bar only. `_state.setdefault('var', [init])` evaluates
        # the `[init]` list every bar even when the key exists (~30 allocs/bar).
        # A trailing `__pb_seeded` sentinel (set last) retries the seed if a
        # partial-mode stub raises mid-block; `if not _state` would skip after a
        # partial write and KeyError on the next bar.
        # EL Inputs are strategy-level constants: bind once into `__pb_inputs`
        # and unpack each bar (kwargs.get of Inputs is otherwise 12 hash lookups
        # of values that never change). market_position / current_contracts /
        # time_s / data2_close stay per-bar — those *do* change.
        #
        # Unindexed persist vars pack into one `_state['__pb_slots']` list and
        # unpack to named locals each bar. 30 dict load+store was ~0.8 s on the
        # FAST window vs ~0.3 s list-unpack + slice-assign (same values).
        # Lookback / series-arg vars stay named lists under `_state[name]`.
        if self.persist and (self._stateful_vars or self._input_inits):
            self._slot_vars = [
                n for n in sorted(self._stateful_vars)
                if n not in self._history_vars
            ]
            # Runnable output keeps lookback lists in a SeriesState so the
            # `(_state['x'] + [x])` series args are O(1) views, not a copy of
            # the whole history every bar (pl_run's bar loop passes its own).
            self._series_state = not self.trace and any(
                n in self._stateful_vars for n in self._history_vars)
            if self._series_state:
                self.emit("_state = kwargs.setdefault('_state', _PbSeriesState())")
            else:
                self.emit("_state = kwargs.setdefault('_state', {})")
            self.emit("if '__pb_seeded' not in _state:")
            self.indent += 1
            for name in sorted(self._history_vars):
                if name not in self._stateful_vars:
                    continue
                seed = self._persist_var_seed(name)
                self.emit(f"_state['{name}'] = [{seed}]")
            if self._slot_vars:
                seeds = ", ".join(self._persist_var_seed(n) for n in self._slot_vars)
                self.emit(f"_state['__pb_slots'] = [{seeds}]")
            if self._input_inits:
                parts = []
                for iname, default_node in self._input_inits:
                    default_val = self.gen_expr(default_node)
                    parts.append(f"kwargs.get({iname!r}, {default_val})")
                inner = ", ".join(parts)
                if len(parts) == 1:
                    inner += ","
                self.emit(f"_state['__pb_inputs'] = ({inner})")
            self.emit("_state['__pb_seeded'] = 1")
            self.indent -= 1
            if self._input_inits:
                names = ", ".join(iname for iname, _ in self._input_inits)
                # A 1-tuple must unpack (`x, = t`); `x = t` would bind the tuple.
                if len(self._input_inits) == 1:
                    self.emit(f"{names}, = _state['__pb_inputs']")
                else:
                    self.emit(f"{names} = _state['__pb_inputs']")
                self._hoisted_input_names = {iname for iname, _ in self._input_inits}
            if self._slot_vars:
                self.emit("_pb_slots = _state['__pb_slots']")
                packed = ", ".join(self._slot_vars)
                if len(self._slot_vars) == 1:
                    self.emit(f"{packed}, = _pb_slots")
                else:
                    self.emit(f"{packed} = _pb_slots")
        # Comments: in clean (trace=False, persist=False) mode, carry the source EL
        # comments through as `#| <verbatim>` lines, anchored (by object identity)
        # to the same statements the reverse emitter uses. Trace and persist-only
        # modes omit them (dumped values / runtime shape must stay stable).
        # Maps are read by gen_stmt; header/footer emitted here.
        self._cmt_lead, self._cmt_trail = {}, {}
        _ftr = ()
        if not self.trace and not self.persist:
            from pl_transpiler.codegen_el import _anchor_comments
            _hdr, _lead, _trail, _ftr = _anchor_comments(
                node['body'], node.get('_comments') or [])
            self._cmt_lead, self._cmt_trail = _lead, _trail
            for txt in _hdr:
                self._emit_comment(txt)

        # Emit the wrapper function body
        for stmt in node['body']:
            self.gen_stmt(stmt)

        if not self.trace and not self.persist:
            for txt in _ftr:
                self._emit_comment(txt)

        # Persist: write this bar's values into `_state`. Unindexed vars pack
        # into `__pb_slots` (one slice-assign, reuse the same list). Lookback /
        # series-arg vars keep a growing named list. Trace: also snapshot every
        # traced name into `_trace` (skipped in the runnable no-trace dialect —
        # that saves ~111 stores/bar).
        if self.persist:
            for name in sorted(self._history_vars):
                if name in self._stateful_vars:
                    self.emit(f"_state['{name}'].append({name})")
            if self._slot_vars:
                packed = ", ".join(self._slot_vars)
                if len(self._slot_vars) == 1:
                    packed += ","
                self.emit(f"_pb_slots[:] = ({packed})")
            # Save arrays directly (not appending — they are mutable lists mutated in place)
            for name in sorted(self._array_vars):
                self.emit(f"_state['{name}'] = {name}")
        if self.trace:
            for name in sorted(self._trace_vars):
                self.emit(f"_trace['{name}'] = {name}")

        # Collect-all fail-loud: if any unimplemented keyword was recorded during the
        # walk, discard the emitted text and raise ONE complete report (no Python is
        # ever returned when an unknown keyword exists — strict all-or-nothing).
        # In partial mode (opt-in) we do NOT raise: each unknown was emitted as an
        # execution-time stub and the output is watermarked below instead.
        if self._unimpl_errors and not self._partial:
            self._raise_unimplemented()

        # Build output
        # Persist/trace run inside run_bar_loop. Passing the engine's kwargs
        # dict by reference (8th positional) avoids a 40-key **unpack copy
        # every bar. The `kwargs=None, **_kw` fallback keeps direct callers
        # (`strategy(*series, _state=..., barnumber=...)`) working.
        # Clean (mirror) dialect stays `**kwargs` — py_front reads that only.
        if self.persist or self.trace:
            sig = (
                "def strategy(open, high, low, close, volume, date, time, kwargs=None, **_kw):\n"
                "    if kwargs is None:\n"
                "        kwargs = _kw\n"
            )
        else:
            sig = (
                "def strategy(open, high, low, close, volume, date, time, **kwargs):\n"
            )
        # Clean (mirror) dialect keeps the list-only coercion — py_front reads
        # that exact prologue. persist/trace also accept array.array / memoryview
        # so a contiguous buffer can be passed in without being wrapped as a
        # 1-element list. Engine still passes Python lists:
        # array[-1] re-boxes a float and measured slower on Audit Ref 3.
        header = (
            sig
            + (
                "    # series: list / array / memoryview (scalars become 1-element lists)\n"
                "    if isinstance(close, (str, bytes)) or not hasattr(close, '__getitem__'):\n"
                "        open, high, low, close, volume, date, time = [open],[high],[low],[close],[volume],[date],[time]\n"
                if (self.trace or self.persist)
                else
                "    # Ensure series params are lists\n"
                "    if not isinstance(close, list): open, high, low, close, volume, date, time = [open],[high],[low],[close],[volume],[date],[time]\n"
            )
            + "    _orders = []\n"
            + "    _plots = {}\n"
            + "    _alerts = []\n"
            + "    _risk = {}\n"
            + "    _commentary = None\n"
            + "    _drawings = []\n"
            + "    _market_position = kwargs.get('market_position', 0)\n"
            + "    _current_contracts = kwargs.get('current_contracts', 0)\n"
            + "    time_s = kwargs.get('time_s', 0)\n"
        )
        if self.has_once:
            header += "    _first_bar = kwargs.get('_first_bar', True)\n"
        if self.trace:
            header += "    _trace = {}\n"
            header += "    _trace['_barnumber'] = kwargs.get('barnumber', 0)\n"
            # The prologue above guarantees indexability for persist/trace
            # (arrays and promoted scalars included), so a bare [-1] is safe.
            header += "    _trace['_date'] = date[-1]\n"
            header += "    _trace['_time'] = time[-1]\n"
            header += "    _trace['_close'] = close[-1]\n"
        # Persist/trace: f32 current-bar OHLCV once. Bare Close/Open/High/Low/Volume
        # then read `_close` etc. (see gen_expr), so `X = Close` is `x = _close`
        # instead of `x = f32(f32(close[-1]))`. Clean dialect is unchanged.
        # After the series coercion above so scalar callers still index [-1].
        if self.persist or self.trace:
            header += (
                "    _open = f32(open[-1])\n"
                "    _high = f32(high[-1])\n"
                "    _low = f32(low[-1])\n"
                "    _close = f32(close[-1])\n"
                "    _volume = f32(volume[-1])\n"
            )

        body = '\n'.join(self.lines)
        return_items = ["'orders': _orders", "'plots': _plots", "'alerts': _alerts", "'risk': _risk"]
        if self.trace:
            return_items.append("'_trace': _trace")
        footer = "\n    return {" + ", ".join(return_items) + "}\n"

        imports = ''
        if self.has_math:
            imports = 'import math\n'
        # Import runtime module (includes f32, el_round, pl_* functions)
        imports += 'from pl_transpiler.runtime.pl_runtime import f32, el_round0, el_round, pl_file\n'
        imports += 'from pl_transpiler.runtime.pl_runtime import *\n'
        # Partial output that actually emitted stubs imports the stub factory
        # explicitly. This line is added ONLY for partial output with N>0 stubs, so
        # the strict transpile stays byte-identical (proven by the corpus baseline).
        if self._partial and self._unimpl_errors:
            imports += 'from pl_transpiler.runtime.pl_runtime import pl_partial_stub\n'
        if self._series_state:
            imports += 'from pl_transpiler.runtime._rt_history import SeriesState as _PbSeriesState\n'
        imports += '__builtin_len = len\n'

        out = imports + header + body + footer

        if self._partial:
            errors = self._sorted_unimpl()
            if errors:
                return self._partial_watermark(errors) + out
            # No unimplemented constructs: output is byte-identical to the strict
            # transpile, preceded by exactly one acknowledgement line.
            return (
                "# PARTIAL TRANSPILE requested — 0 unimplemented constructs; "
                "output identical to the strict transpile.\n" + out
            )
        return out

    def _expr_refs(self, node, names):
        """Collect into `names` every user-variable referenced in expr `node`
        (bare ident `Var` and bar-ref `Var[n]`)."""
        if not isinstance(node, dict):
            return
        t = node.get('type')
        if t == 'ident':
            names.add(node.get('name'))
        elif t == 'bar_ref':
            series = node.get('series') or {}
            if series.get('type') == 'ident':
                names.add(series.get('name'))
            self._expr_refs(node.get('index'), names)
        # Recurse into child expression nodes generically.
        for key in ('left', 'right', 'operand', 'cond', 'msg', 'value', 'init',
                    'start', 'end', 'index'):
            if key in node:
                self._expr_refs(node[key], names)
        for key in ('args', 'decls'):
            for child in node.get(key, []) or []:
                self._expr_refs(child, names)

    def _collect_double_accums(self, stmts):
        """Walk all statements; mark numeric vars assigned from an expression that
        references themselves (self-referential cross-bar accumulators)."""
        for stmt in stmts or []:
            if not isinstance(stmt, dict):
                continue
            if stmt.get('type') == 'assign':
                target = stmt.get('target', {})
                if target.get('type') == 'ident':
                    tname = target.get('name')
                    if tname in self._numeric_vars:
                        refs = set()
                        self._expr_refs(stmt.get('value'), refs)
                        if tname in refs:
                            self._double_accum_vars.add(tname)
            # Recurse into nested bodies (if/else/for/while).
            for key in ('body', 'else_body'):
                self._collect_double_accums(stmt.get(key))

    def _collect_history_vars(self, node, series_context=False):
        """Mark stateful vars whose persist slot must stay a lookback list.

        Mirrors gen_expr: a bare ident emitted under `_series_context` becomes
        `(_state['x'] + [x])`, and `Var[n]` (n != 0) indexes `_state['x']`.
        Every other Variable is a scalar: seed once, overwrite each bar.
        """
        if isinstance(node, list):
            for x in node:
                self._collect_history_vars(x, series_context)
            return
        if not isinstance(node, dict):
            return
        t = node.get('type')
        if t == 'ident':
            n = node.get('name')
            if series_context and n in self._stateful_vars:
                self._history_vars.add(n)
            return
        if t == 'bar_ref':
            series = node.get('series') or {}
            index = node.get('index')
            if series.get('type') == 'ident' and series.get('name') in self._stateful_vars:
                needs = True
                if isinstance(index, dict) and index.get('type') == 'number':
                    try:
                        needs = int(float(index.get('value', '1'))) != 0
                    except (TypeError, ValueError):
                        needs = True
                if needs:
                    self._history_vars.add(series['name'])
            self._collect_history_vars(series, True)
            self._collect_history_vars(index, False)
            return
        if t == 'call':
            name = (node.get('name') or '').lower()
            child_ctx = series_context or (name in self._SERIES_FUNCS)
            for a in node.get('args') or []:
                self._collect_history_vars(a, child_ctx)
            return
        for v in node.values():
            if isinstance(v, (dict, list)):
                self._collect_history_vars(v, series_context)

    def _persist_var_seed(self, name):
        """First-bar seed expression for one persist var (scalar slot or list)."""
        init_val = self._state_inits.get(name, '0')
        if self._data_refs.get(name):
            seed = "kwargs.get('data2_close', close[-1])"
            if (name in self._numeric_vars
                    and name not in self._double_accum_vars):
                seed = f"f32({seed})"
                self._need_f32_import = True
            return seed
        return self._persist_seed_expr(name, init_val)

    def _persist_seed_expr(self, name, init_val):
        """First-bar seed for a persist var.

        Numeric non-accumulators are f32-narrowed at seed so the per-bar load
        does not re-wrap (f32 is idempotent; the old load wrap was 20 ctypes
        calls/bar on Audit Ref 3). Partial-mode stubs stay bare so they raise.
        """
        if 'pl_partial_stub' in init_val:
            return init_val
        if name in self._numeric_vars and name not in self._double_accum_vars:
            self._need_f32_import = True
            return f"f32({init_val})"
        return init_val

    def _collect_assigned_targets(self, node, out):
        """Recursively add into `out` the name of every ident that appears as an
        assignment TARGET anywhere in the AST (at any nesting depth). Used by the
        bare-keyword flag so a name defined purely by assignment (never declared) is
        treated as a known local rather than an unimplemented keyword."""
        if isinstance(node, dict):
            if node.get('type') == 'assign':
                tgt = node.get('target')
                if isinstance(tgt, dict) and tgt.get('type') == 'ident':
                    out.add(tgt['name'])
                # Stochastic-style multi-output: the trailing call args are output
                # var names the assignment defines.
                val = node.get('value')
                if (isinstance(val, dict) and val.get('type') == 'call'
                        and val.get('name') == 'stochastic'):
                    for a in val.get('args', [])[7:]:
                        if isinstance(a, dict) and a.get('type') == 'ident':
                            out.add(a['name'])
            for v in node.values():
                self._collect_assigned_targets(v, out)
        elif isinstance(node, list):
            for x in node:
                self._collect_assigned_targets(x, out)

    def _collect_read_names(self, node, out):
        """Recursively add into `out` every identifier that appears in a READ (value)
        position anywhere in the AST — a bare ident, or the series name of a bar_ref.
        An assignment TARGET is a WRITE, not a read, so its target ident is skipped
        (but the assignment's RHS, and a subscript index on a bar_ref target `Arr[i]`,
        ARE reads and are walked). Used by the write-only lvalue guard so that only an
        assignment target that is ALSO read somewhere is whitelisted as a legitimate
        local — a write-only unimplemented EL keyword (`AvgWinTrade = Close`) is not."""
        if isinstance(node, dict):
            t = node.get('type')
            if t == 'ident':
                if node.get('name'):
                    out.add(node['name'])
                return
            if t == 'bar_ref':
                series = node.get('series') or {}
                if series.get('type') == 'ident':
                    if series.get('name'):
                        out.add(series['name'])
                else:
                    self._collect_read_names(series, out)
                self._collect_read_names(node.get('index'), out)
                return
            if t == 'assign':
                self._collect_read_names(node.get('value'), out)
                tgt = node.get('target')
                if isinstance(tgt, dict) and tgt.get('type') == 'bar_ref':
                    self._collect_read_names(tgt.get('index'), out)
                return
            for v in node.values():
                self._collect_read_names(v, out)
        elif isinstance(node, list):
            for x in node:
                self._collect_read_names(x, out)

    def _ordered_ident_events(self, node, events):
        """Append (kind, name) identifier events in EVALUATION order. kind is
        'read' for an identifier used as a value; 'cleandef' for an assignment
        whose target ident is NOT read on its own RHS (a defining write that
        establishes a value independent of the target's prior state). An `assign`
        evaluates its RHS (reads) before the target is written, and a control-flow
        node evaluates its condition/bounds (reads, in source order) before its
        body — the generic in-order recursion preserves both. Feeds the read-before-write
        rescue analysis (a genuine local is cleanly defined before its first read;
        a read-before-write or self-referential unimplemented keyword is not)."""
        if isinstance(node, list):
            for x in node:
                self._ordered_ident_events(x, events)
            return
        if not isinstance(node, dict):
            return
        t = node.get('type')
        if t == 'ident':
            if node.get('name'):
                events.append(('read', node['name']))
            return
        if t == 'bar_ref':
            series = node.get('series') or {}
            if series.get('type') == 'ident':
                if series.get('name'):
                    events.append(('read', series['name']))
            else:
                self._ordered_ident_events(series, events)
            self._ordered_ident_events(node.get('index'), events)
            return
        if t == 'assign':
            # RHS is evaluated (reads) before the target is assigned.
            self._ordered_ident_events(node.get('value'), events)
            rhs_reads = set()
            self._collect_read_names(node.get('value'), rhs_reads)
            tgt = node.get('target')
            if isinstance(tgt, dict) and tgt.get('type') == 'ident' and tgt.get('name'):
                name = tgt['name']
                # A "clean" define establishes a value not derived from the
                # target's own prior state (target not read on the RHS). A self-
                # referential write (`X = X + 1`) emits no cleandef — the RHS read
                # event above already records X (and marks it broken if undefined).
                if name not in rhs_reads:
                    events.append(('cleandef', name))
            elif isinstance(tgt, dict) and tgt.get('type') == 'bar_ref':
                # Array/element write: the subscript index is a read; the series
                # name is an array, handled via declarations, not a scalar define.
                self._ordered_ident_events(tgt.get('index'), events)
            return
        for v in node.values():
            self._ordered_ident_events(v, events)

    def _rescuable_assign_locals(self, node, assigned_and_read):
        """From the set of names that are BOTH assigned and read (`_assigned &
        _read`), return only those safe to treat as genuine locals-by-assignment.

        Excluded (so the strict fail-loud path still flags them):
          * any name otherwise recognized as a builtin/reserved keyword or a
            resolvable pl_ target (e.g. FastD) — a write to it is not a local decl;
          * any name whose first appearance in evaluation order is a READ, or whose
            only defines are self-referential (`X = X + 1` with no prior clean
            define) — it has no value-establishing write (FakeA read-before-write,
            AvgWinTrade self-accumulate), so it is a read of an undefined name.
        Kept: names with a clean defining write BEFORE their first read — exactly
        the flattened nested-`Variables:` locals the reverse path emits as bare
        `x = 0; ...; y = x`."""
        if not assigned_and_read:
            return set()
        from pl_transpiler.parser import _is_known_name, BUILTIN_FUNC_MAP  # noqa: F811
        from pl_transpiler import catalog
        candidates = set()
        for name in assigned_and_read:
            # FAIL-CLOSED: a name that is a RECOGNIZED EL keyword
            # (catalog signature present — e.g. a strategy-performance / report
            # keyword such as AvgBarsWinTrade) is read-only in EL and can NEVER be a
            # genuine round-tripped `Variables:` local (EL forbids declaring a var
            # named after a reserved keyword). Rescuing it as a local on the strength
            # of a `Kw = <expr>` write (which EL itself rejects) silently emits a
            # wrong value and drops it from the collect-all report. Exclude any
            # catalog-known keyword so a write+read of one still fails loud.
            if (name in self._declared_all
                    or name in BUILTIN_FUNC_MAP
                    or f'pl_{name}' in _available_pl_targets()
                    or catalog.get(name) is not None
                    or _is_known_name(name, self._declared_all)):
                continue  # otherwise-known keyword — never rescue as a local
            candidates.add(name)
        if not candidates:
            return set()
        events = []
        self._ordered_ident_events(node, events)
        clean_defined = set()
        broken = set()
        for kind, name in events:
            if name not in candidates:
                continue
            if kind == 'read':
                if name not in clean_defined:
                    broken.add(name)
            else:  # 'cleandef'
                clean_defined.add(name)
        return (candidates & clean_defined) - broken


def generate(ast, trace=False, partial=False, persist=None):
    """Walk AST and emit Python source string.
    If trace=True, generated code captures per-bar variable values into _trace dict.
    persist: cross-bar `_state` container. None (default) follows `trace`, so
    existing callers are unchanged. persist=True with trace=False emits a
    runnable backtest without `_trace` stores (the clean mirror dialect is
    persist=False and is NOT this path).
    If partial=True (opt-in), unimplemented EL keywords become execution-time
    stubs (pl_partial_stub) under a NOT-FAITHFUL watermark instead of raising at
    transpile time; the strict default (partial=False) is unchanged."""
    cg = CodeGen(trace=trace, partial=partial, persist=persist)
    return cg.gen_program(ast)
