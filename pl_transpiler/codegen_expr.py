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

"""Expression generation for CodeGen (mixin; see codegen.py)."""

from typing import TYPE_CHECKING, Any, NoReturn, Optional, Union

from ._types import Node
from .codegen_tables import _SERIES_PARAMS, BUILTIN_FUNC_MAP, COMPARE_OPS
from .codegen_util import (
    _OHLCV_F32_NAMES,
    _runtime_bound_bare_names,
    available_pl_targets,
    call_arity_problem,
)
from .errors import UnimplementedKeywordError, unimplemented_message

# Computed reserved words that depend only on the bar's data columns.
_BARE_COMPUTED_PRICES = frozenset((
    'range', 'truerange', 'medianprice', 'avgprice', 'truehigh', 'truelow',
    'typicalprice', 'weightedclose',
))

# Position keywords whose positions-ago form (EntryDate(n), ...) reads the
# closed-position ledger via pl_pos_ago (keys of runtime _rt_core._POS_AGO_FIELDS).
_POS_AGO_KWARGS = frozenset((
    'entry_date', 'entry_time', 'entry_price',
    'exit_date', 'exit_time', 'exit_price', 'maxshares',
))


# EL reserved words backed by an optional bar-data column -> EL spelling.
_BAR_COLUMN_WORDS = {
    'ticks': 'Ticks', 'upticks': 'UpTicks',
    'downticks': 'DownTicks', 'openint': 'OpenInt',
}

class _ExprMixin:
    if TYPE_CHECKING:
        # Host interface provided by CodeGen (see codegen.py).
        trace: bool
        persist: bool
        has_math: bool
        _series_context: bool
        _partial: bool
        _need_f32_import: bool
        _lag_sites: int
        _cur_stmt_line: Optional[int]
        _trace_vars: set[str]
        _stateful_vars: set[str]
        _numeric_vars: set[str]
        _array_vars: set[str]
        _declared_all: set[str]
        _input_inits: list[tuple[str, Node]]
        _unimpl_errors: list[dict[str, Any]]

    # ==== Expression Generation ====
    def gen_expr(self, node: Node) -> str:
        ntype = node['type']

        if ntype == 'number':
            val: str = node['value']
            # Strip leading zeros from integer literals to avoid Python octal errors
            # but preserve "0" itself and floats like "0.5"
            if '.' not in val:
                val = val.lstrip('0') or '0'
            return val

        if ntype == 'string':
            return repr(node['value'])

        if ntype == 'boolean':
            return 'True' if node['value'] else 'False'

        if ntype == 'ident':
            name: str = node['name']
            # Data series params: emit current-bar value unless in series context
            if name in _SERIES_PARAMS and not self._series_context:
                # Wrap OHLCV data in f32 for EL float32 fidelity (ad, #U10).
                # Persist/trace: the wrapper prologue already bound `_close` etc.
                if name in _OHLCV_F32_NAMES:
                    if self.persist or self.trace:
                        return f"_{name}"
                    return f"f32({name}[-1])"
                return f"{name}[-1]"
            # In series context, user variables should pass their history list
            # Include the current bar's computed value (state hasn't been appended yet)
            if self._series_context and name in self._stateful_vars:
                return f"(_state['{name}'] + [{name}])"
            # Bare reserved words that compute values from bar data
            _BARE_COMPUTED = {
                'truerange': "pl_true_range(high[-1], low[-1], (close[-2] if close[-2] != 0.0 else open[-1]))",
                'medianprice': "((high[-1] + low[-1]) / 2.0)",
                'range': "(high[-1] - low[-1])",
                'avgprice': "((open[-1] + high[-1] + low[-1] + close[-1]) / 4.0)",
                'truehigh': "max(high[-1], (close[-2] if close[-2] != 0.0 else open[-1]))",
                'truelow': "min(low[-1], (close[-2] if close[-2] != 0.0 else open[-1]))",
                'typicalprice': "((high[-1] + low[-1] + close[-1]) / 3.0)",
                'weightedclose': "((high[-1] + low[-1] + 2 * close[-1]) / 4.0)",
                'currentdate': "kwargs.get('current_date', 0)",
                'currenttime': "kwargs.get('current_time', 0)",
                # Seconds-resolution wall-clock reserved words. Used bare (no
                # parens) they reach this resolver; without an entry they would
                # emit a raw undefined identifier and raise NameError, zeroing
                # the whole trace dict. Resolve to their runtime fns.
                'currenttime_s': "pl_currenttime_s()",
                'currentdate_s': "pl_currentdate_s()",
                'ticks': "pl_bar_column(kwargs, 'ticks', 'Ticks')",
                'upticks': "pl_bar_column(kwargs, 'upticks', 'UpTicks')",
                'downticks': "pl_bar_column(kwargs, 'downticks', 'DownTicks')",
                'openint': "pl_bar_column(kwargs, 'openint', 'OpenInt')",
            }
            # A user-DECLARED name always WINS over a builtin of the same name —
            # including a name declared in a NESTED Variables:/Inputs:/Arrays: block
            # (or defined by a plain assignment) that collides with a bare-computed
            # reserved word (Range/MedianPrice/...) or a __marker__ builtin
            # (FastD/Floor/CCI/...). self._declared_all is the recursive whole-AST
            # declared+assigned oracle; the top-level-only trace/state/array/numeric
            # sets alone MISS a nested declaration, which would then be mis-resolved
            # to the builtin expansion (a discarded decl / SyntaxError / wrong value).
            # An UNDECLARED bare builtin still resolves to its expansion below because
            # user_decl stays False (e.g. `Value1 = Range;` with no Variables: Range).
            user_decl = (name in self._declared_all
                         or name in self._trace_vars or name in self._stateful_vars
                         or name in self._array_vars or name in self._numeric_vars)
            if name in _BARE_COMPUTED and not user_decl:
                return _BARE_COMPUTED[name]
            # EL reserved-word functions can be referenced bare (no parens),
            # e.g. `X = Symbol ;` or `X = Sess1EndTime ;`. Without parens they
            # reach this resolver; emitting the raw identifier would produce
            # undefined-name code that raises NameError at runtime and zeroes
            # the ENTIRE _trace dict (every traced column then falls through to
            # '0'). Route any name with a known builtin mapping through the
            # call generator as a zero-arg call so it resolves to its inline
            # expansion or pl_* runtime fn, exactly like the parenthesized form.
            # Guard: skip user-declared variables (user_decl) — a script may declare
            # a var whose name collides with a builtin (e.g. PrevClose), and it can
            # appear as an assignment TARGET; emitting pl_prevclose() there is a
            # syntax error. User vars always take precedence and pass through.
            if name in BUILTIN_FUNC_MAP and not user_decl:
                return self._gen_call({'name': name, 'args': []})
            # A bare identifier that resolves to nothing — not a declared
            # var/input/array (at ANY depth), not a series/bar keyword, not a
            # recognized reserved word / scratch var (Value*/Condition*), and not
            # a resolvable pl_* builtin — is an UNIMPLEMENTED EL keyword (e.g. a
            # bare `AvgWinTrade`). Emitting the raw lowercased name produces
            # undefined-name code (silent NameError at runtime) and leaves the
            # collect-all report incomplete. Treat it like any other unimplemented
            # keyword: strict collects {name,line} (same dedup/sort/message
            # contract) and raises; partial emits an argless execution-time stub.
            #
            # Reserved names: a name the parser recognizes as a reserved identifier passes
            # _is_known_bare_ident, but returning it raw only WORKS when that raw
            # lowercased name actually binds in the generated scope. A reserved
            # word with no pl_ target and no runtime binding (bare `Pi`, an
            # order-syntax word mis-used as a value, a pl_-only keyword) would emit
            # an undefined identifier -> NameError, silently zeroing the trace.
            # Treat it, too, as an unimplemented keyword (fail loud / stub).
            if not self._is_known_bare_ident(name) or not self._bare_raw_resolves(name):
                line = node.get('_line') or self._cur_stmt_line
                self._unimpl_errors.append({'name': name, 'line': line})
                if self._partial:
                    return f"pl_partial_stub({name!r}, {line!r})()"
            return name

        if ntype == 'binop':
            before = len(self._unimpl_errors)
            left = self.gen_expr(node['left'])
            right = self.gen_expr(node['right'])
            op = node['op']
            # EL/PL `and`/`or` are NON-short-circuit — BOTH operands are
            # always evaluated. Python's `and`/`or` short-circuit, so a stub on the
            # skipped side (e.g. `(Close < 0) and FakeF(Close)` where the left is
            # False) would be BYPASSED and the expression would silently yield a
            # value instead of raising. Both operands were already walked above (so
            # every nested unimplemented keyword is recorded for the watermark); in
            # PARTIAL mode, if either side collected an unimplemented entry, collapse
            # the whole boolean to a single argless execution-time stub named after
            # the unimplemented operand so it ALWAYS raises when evaluated. Strict
            # mode is unaffected (any recorded entry aborts the whole transpile).
            if op in ('and', 'or') and self._partial and len(self._unimpl_errors) > before:
                e = self._unimpl_errors[before]
                return f"pl_partial_stub({e['name']!r}, {e['line']!r})()"
            return f"({left} {op} {right})"

        if ntype == 'compare':
            left = self.gen_expr(node['left'])
            right = self.gen_expr(node['right'])
            op = COMPARE_OPS.get(node['op'], node['op'])
            return f"({left} {op} {right})"

        if ntype == 'unaryop':
            operand = self.gen_expr(node['operand'])
            op = node['op']
            if op == 'not':
                return f"(not {operand})"
            return f"({op}{operand})"

        if ntype == 'position_kw':
            kwarg = node['kwarg']
            # Per-bar data columns the bar data may lack: read them through
            # pl_bar_column so a missing column stops the run (strict) instead
            # of reading as 0.
            if kwarg in _BAR_COLUMN_WORDS:
                return f"pl_bar_column(kwargs, '{kwarg}', '{_BAR_COLUMN_WORDS[kwarg]}')"
            # Some position keywords have non-zero defaults.
            # AlertEnabled is False during a backtest/data export (alerts only
            # fire on the live last bar), which is the context every GT capture
            # was recorded in — so default it False to match (GTA6's AlEn col).
            defaults = {
                'alert_enabled': 'False',
                'check_alert': 'True',
                'lastbaronchartex': 'False',
                # String-valued instrument properties.
                'symbol': "''",
                'symbolname': "''",
                'description': "''",
            }
            default = defaults.get(kwarg, '0')
            return f"kwargs.get('{kwarg}', {default})"

        if ntype == 'position_kw_call':
            # 2-arg form e.g. PosTradeProfit(PosAgo, TradeNumber).
            # Evaluate args (so referenced names resolve) and look up the
            # per-trade value supplied via kwargs, defaulting to 0.
            kwarg = node['kwarg']
            args = ', '.join(self.gen_expr(a) for a in node.get('args', []))
            return f"pl_pos_trade_field(kwargs, '{kwarg}', {args})"

        if ntype == 'mc_kw':
            kwarg = node['kwarg']
            defaults = {
                'time_s': '0',
                'bartype_ex': '2',
            }
            default = defaults.get(kwarg, '0')
            return f"kwargs.get('{kwarg}', {default})"

        if ntype == 'data_of':
            series = node['series']
            data_num = node['data_num']
            # Emit Data<N> series accessor; falls back to Data1 when Data<N> not provided
            return f"kwargs.get('data{data_num}_{series}', {series}[-1])"

        if ntype == 'call':
            return self._gen_call(node)

        if ntype == 'bar_ref':
            return self._gen_bar_ref(node)

        raise ValueError(f"Unknown expression type: {ntype}")

    # Functions whose arguments should receive full series (not [-1])
    _SERIES_FUNCS = {
        'average', 'averagefc', 'averagetfc', 'weightedaverage', 'xaverage',
        'highest', 'lowest', 'highestbar', 'lowestbar',
        'rsi', 'momentum', 'summation', 'standarddev', 'stddev', 'variance',
        'correlation', 'linearregvalue', 'linearregslope',
        'macdvalue', 'macd', 'macddiff', 'macdsignal',
        'roc', 'rateofchange', 'crossesabove', 'crossesbelow',
        'waverage', 'countif', 'bollingerband', 'stochastic',
        'linearregangle',
    }

    # Series functions whose first argument is the price series. EL evaluates
    # that argument on every bar, so an expression (``(High + Low) / 2``), an
    # offset (``Close[5]``) or a computed word (``Range``) passed there gets a
    # hidden per-site history, like a crosses operand (see _collect_arg_sites).
    _PRICE_ARG_FUNCS = frozenset((
        'average', 'averagefc', 'averagetfc', 'weightedaverage', 'waverage',
        'xaverage', 'highest', 'lowest', 'highestbar', 'lowestbar',
        'summation', 'momentum', 'roc', 'rateofchange', 'rsi',
        'standarddev', 'stddev', 'variance',
        'linearregvalue', 'linearregslope', 'linearregangle',
    ))

    def _is_user_name(self, name: Optional[str]) -> bool:
        """True if ``name`` is declared or assigned by the program (it then
        wins over a reserved word of the same name)."""
        return (name in self._declared_all
                or name in self._trace_vars or name in self._stateful_vars
                or name in self._array_vars or name in self._numeric_vars)

    def _price_arg_site(self, call: Node) -> Optional[str]:
        """The hidden history for ``call``'s price argument, or None.

        A price argument that needs one but cannot have it (its value can
        change within the bar) is recorded as an error: the call would
        otherwise crash at run time."""
        name = call.get('name')
        args = call.get('args') or []
        if not self.persist or name not in self._PRICE_ARG_FUNCS or not args:
            return None
        arg = args[0]
        site = getattr(self, '_arg_sites', {}).get(id(arg))
        if site is not None:
            return f"_state['__pb_a{site[0]}']"
        site = getattr(self, '_call_sites', {}).get(id(arg))
        if site is not None:
            # Append this bar's value where the call runs, then pass the
            # history: the same as assigning the argument to a variable just
            # before the statement and passing that variable.
            old_ctx = self._series_context
            self._series_context = False
            value = self.gen_expr(arg)
            self._series_context = old_ctx
            self._need_f32_import = True
            hist = f"_state['__pb_c{site}']"
            return f"({hist}.append(f32({value})) or {hist})"
        if self._needs_arg_site(arg):
            from pl_transpiler.codegen_el import emit_expr
            line = call.get('_line') or self._cur_stmt_line
            self._unimpl_errors.append({
                'name': name, 'line': line, 'kind': 'unsupported',
                'detail': (f"price argument {emit_expr(arg)!r} reads a variable "
                           f"and the call does not run exactly once on every bar; "
                           f"assign it to a declared variable and pass that "
                           f"variable instead")})
        return None

    def _needs_arg_site(self, node: Node) -> bool:
        """True if a price argument needs a hidden history: an offset, a
        computed word, an expression or a nested call. Only a bare series
        (``Close``, a variable) or a constant is passed as it is."""
        t = node.get('type')
        if t == 'bar_ref':
            series_type: Optional[str] = node['series'].get('type')
            return series_type != 'position_kw'
        if t == 'ident':
            return (node.get('name') in _BARE_COMPUTED_PRICES
                    and not self._is_user_name(node.get('name')))
        return t in ('binop', 'unaryop', 'compare', 'call')

    def _gen_lagged_range(self, name: str, index: Node) -> str:
        """``Range[n]`` = High[n] - Low[n]; ``TrueRange[n]`` also uses Close[n + 1]."""
        def col(c: str, idx: Node) -> str:
            return self._gen_bar_ref({'type': 'bar_ref',
                                      'series': {'type': 'ident', 'name': c},
                                      'index': idx})
        hi, lo = col('high', index), col('low', index)
        if name == 'range':
            return f"({hi} - {lo})"
        if index.get('type') == 'number':
            prev: Node = {'type': 'number', 'value': str(int(index['value']) + 1)}
        else:
            prev = {'type': 'binop', 'op': '+', 'left': index,
                    'right': {'type': 'number', 'value': '1'}}
        return f"pl_true_range({hi}, {lo}, {col('close', prev)})"

    def _is_known_bare_ident(self, name: str) -> bool:
        """True if a bare (parenless) identifier reaching gen_expr's ident fallback
        is a LEGITIMATE name and must pass through as-is (never flagged).

        Recognized: any user-declared var/input/array/for name at ANY depth
        (self._declared_all, collected recursively), the codegen's own trace/state
        sets, series params, EL builtins (BUILTIN_FUNC_MAP), any pl_* runtime target,
        the boolean words, and every reserved/scratch identifier the parser's
        semantic oracle (`is_known_name`) already treats as known — Value0..Value99,
        Condition0..Condition99, marketposition, order words, colors/styles, etc.
        Anything else is an unimplemented EL keyword."""
        if (name in self._declared_all
                or name in self._trace_vars or name in self._stateful_vars
                or name in self._array_vars or name in self._numeric_vars
                or name in BUILTIN_FUNC_MAP
                or name in ('true', 'false')
                or f'pl_{name}' in available_pl_targets()):
            return True
        from pl_transpiler.semantic import is_known_name
        return is_known_name(name, self._declared_all)

    def _bare_raw_resolves(self, name: str) -> bool:
        """True when a bare identifier reaching the ident fallback can be emitted
        verbatim because the raw lowercased name actually binds in the generated
        strategy() scope: a generated local (user-declared/assigned var, series
        state, array, numeric), an auto-declared scratch var (Value*/Condition*),
        or a function param / prologue internal / runtime-imported name / Python
        builtin (_runtime_bound_bare_names). A reserved word the parser lists but
        which binds to nothing here returns False -> the caller fails it loud
        instead of emitting an undefined name."""
        if (name in self._declared_all
                or name in self._trace_vars or name in self._stateful_vars
                or name in self._array_vars or name in self._numeric_vars):
            return True
        from pl_transpiler.semantic import _SCRATCH_VARS
        if name in _SCRATCH_VARS:
            return True
        return name in _runtime_bound_bare_names()

    def _require_pl_impl(self, name: str, node: Optional[Node] = None) -> str:
        """Validate that a generic `pl_<name>` target resolves in the runtime.
        On a miss, RECORD the unimplemented keyword (name + nearest source line) into
        the per-generate collection and return the emittable `pl_<name>` target string
        so the walk continues — the whole program is scanned and every unimplemented
        keyword is reported at once at the end of generate(). The emitted text is
        discarded whenever any error was recorded (strict all-or-nothing preserved)."""
        target = f"pl_{name}"
        if target not in available_pl_targets():
            line = None
            if isinstance(node, dict):
                line = node.get('_line')
            if line is None:
                line = self._cur_stmt_line
            self._unimpl_errors.append({'name': name, 'line': line})
            # Partial mode: emit an execution-time stub callable instead of the
            # generic pl_<name> target. Callers wrap the return in `(args...)`, so
            # `pl_partial_stub('<name>', <line>)` becomes `pl_partial_stub(...)(args)`
            # — a call that RAISES UnimplementedKeywordError only if it is evaluated.
            if self._partial:
                return f"pl_partial_stub({name!r}, {line!r})"
        return target

    def _lagged(self, s: str, offset: Union[int, str], slice_zero: bool = False) -> str:
        """``s[:-offset]`` for ``Func(s)[offset]``, emitted as a per-site
        ``_pb_lag`` so the Python path does not copy the history every bar.
        A literal offset of 0 passes ``s`` through unless ``slice_zero``
        (the ADX family has always sliced, giving an empty series)."""
        if isinstance(offset, int) and offset <= 0 and not slice_zero:
            return s
        self._lag_sites += 1
        return f"_pb_lag({self._lag_sites}, {s}, {offset})"

    def _impl_call(self, name: str, node: Node, args: list[str]) -> str:
        """Emit the call expression for a builtin target `pl_<name>(args)`.

        Delegates to _require_pl_impl (which records the keyword on a miss). In
        partial mode, when the target is an execution-time stub, the stub is
        invoked with NO arguments — `pl_partial_stub('<name>', <line>)()`
        (the original argument expressions must not survive into the stub call;
        they would evaluate BEFORE the stub raises and can crash with an
        unrelated TypeError). The arg ASTs were already WALKED by the caller
        (building ``args``), so nested unknowns are still collected and the
        watermark stays complete; only the emitted arg strings are dropped."""
        target = self._require_pl_impl(name, node)
        if self._partial and target.startswith('pl_partial_stub('):
            return f"{target}()"
        return f"{target}({', '.join(args)})"

    def _sorted_unimpl(self) -> list[dict[str, Any]]:
        """Deduplicate + sort the recorded unimplemented keywords: dedup by
        (name, line); sort by (line, name) with None lines last. Returns the list
        of ``{'name','line'}`` dicts shared by the raise path (strict) and the
        watermark/manifest path (partial), so both see the identical ordering."""
        seen: set[tuple[Any, Any]] = set()
        errors: list[dict[str, Any]] = []
        for e in self._unimpl_errors:
            key = (e['name'], e['line'])
            if key in seen:
                continue
            seen.add(key)
            out: dict[str, Any] = {'name': e['name'], 'line': e['line']}
            if e.get('kind'):
                out['kind'] = e['kind']
            if e.get('detail'):
                out['detail'] = e['detail']
            errors.append(out)
        errors.sort(key=lambda e: (e['line'] is None, e['line'] or 0, e['name']))
        return errors

    def _partial_watermark(self, errors: list[dict[str, Any]]) -> str:
        """Build the partial-mode watermark comment block that PREFIXES the
        generated file when N>0 stubs were emitted. Names every stub (same dedup/
        sort as the strict report) and states loudly that the output is not faithful."""
        from pl_transpiler.errors import format_error_lines
        n = len(errors)
        bar = "# " + "=" * 70
        lines = [
            bar,
            "# PARTIAL TRANSPILE — NOT FAITHFUL",
            f"# {n} unimplemented construct(s) replaced with execution-time stubs.",
            "# do not trust backtest results",
            "# Each stub raises UnimplementedKeywordError when evaluated.",
            "# Unimplemented constructs:",
        ]
        for txt in format_error_lines(errors):
            lines.append(f"#   {txt}")
        lines.append(bar)
        return "\n".join(lines) + "\n"

    def _raise_unimplemented(self) -> NoReturn:
        """Deduplicate + sort the recorded unimplemented keywords and raise ONE
        UnimplementedKeywordError. Dedup by (name, line); sort by (line, name) with
        None lines last. Single-error message is the short one-line format;
        N>1 uses the multi-keyword report format."""
        errors = self._sorted_unimpl()
        message = unimplemented_message(errors)
        raise UnimplementedKeywordError(message, errors=errors)

    def _gen_call(self, node: Node) -> str:
        # In PARTIAL mode several inline expansions in _gen_call_impl DROP,
        # RELOCATE, or STRING-INTERPOLATE their argument text — CountIf keeps only
        # args[1]; EntryName/ExitName/BaseDataNumber/CurrentDataNumber/CurrentDate/
        # CurrentTime/Recalculate ignore their args; BarNumberOfData interpolates an
        # arg into a single-quoted f-string. If a nested argument collected an
        # unimplemented keyword (its stub), that stub would then be dropped (the run
        # COMPLETES with a wrong value) or its quotes would break the literal (a
        # compile-time SyntaxError). Collapse the WHOLE call to a single argless
        # `pl_partial_stub('<UNIMPL>', <line>)()` naming an actually-UNIMPLEMENTED
        # keyword collected from the subtree (the first one), so the stub occupies a
        # standalone evaluated position and the run RAISES UnimplementedKeywordError.
        # Only override when the ENCLOSING keyword is itself IMPLEMENTED: when it is
        # unimplemented, _gen_call_impl already returns a correct stub naming IT
        # (result starts with 'pl_partial_stub('), which we keep — a user must never
        # see an implemented keyword reported as unimplemented. The arguments are
        # still fully WALKED inside _gen_call_impl, so every nested unknown is recorded
        # in the watermark. Strict mode is untouched (guarded on self._partial).
        before = len(self._unimpl_errors)
        result = self._gen_call_impl(node)
        if (self._partial and len(self._unimpl_errors) > before
                and not (isinstance(result, str)
                         and result.startswith('pl_partial_stub('))):
            e = self._unimpl_errors[before]
            return f"pl_partial_stub({e['name']!r}, {e['line']!r})()"
        return result

    def _gen_call_impl(self, node: Node) -> str:
        name: str = node['name']
        problem = call_arity_problem(name, len(node['args']))
        if problem is not None:
            line = node.get('_line') or self._cur_stmt_line
            self._unimpl_errors.append({'name': name, 'line': line,
                                        'kind': 'arity', 'detail': problem})
            if self._partial:
                return f"pl_partial_stub({name!r}, {line!r})()"
        sites = getattr(self, '_cross_sites', None)
        if sites and name in ('crossesabove', 'crossesbelow'):
            old_ctx = self._series_context
            self._series_context = True
            args = [f"_state['__pb_x{sites[id(a)][0]}']" if id(a) in sites
                    else self.gen_expr(a) for a in node['args']]
            self._series_context = old_ctx
            return f"{BUILTIN_FUNC_MAP[name]}({', '.join(args)})"
        # Enable series context for functions that accept series args
        old_ctx = self._series_context
        if name in self._SERIES_FUNCS:
            self._series_context = True
        args = [self.gen_expr(a) for a in node['args']]
        self._series_context = old_ctx
        site = self._price_arg_site(node)
        if site is not None:
            args[0] = site

        # Special cases
        if name == 'power' and len(args) == 2:
            return f"({args[0]} ** {args[1]})"

        if name in ('log', 'sqrt'):
            self.has_math = True

        py_name = BUILTIN_FUNC_MAP.get(name)

        # Inline expansions for simple math functions
        if py_name == '__intportion__' and len(args) == 1:
            return f"int({args[0]})"
        if py_name == '__fracportion__' and len(args) == 1:
            return f"({args[0]} - int({args[0]}))"
        if py_name == '__sign__' and len(args) == 1:
            a = args[0]
            return f"(1 if {a} > 0 else (-1 if {a} < 0 else 0))"
        if py_name == '__square__' and len(args) == 1:
            return f"({args[0]} ** 2)"
        if py_name == '__numtostr__' and len(args) >= 2:
            return f'f"{{{args[0]}:.{{{args[1]}}}f}}"'
        if py_name == '__strtonum__' and len(args) == 1:
            return f"float({args[0]})"

        # Technical indicators that need high/low/close injected
        if py_name == '__cci__' and len(args) == 1:
            return f"pl_cci(high, low, close, {args[0]})"
        if py_name == '__atr__' and len(args) == 1:
            return f"pl_atr(high, low, close, {args[0]})"
        if py_name == '__adx__' and len(args) == 1:
            return f"pl_adx(high, low, close, {args[0]})"
        if py_name == '__adxr__' and len(args) == 1:
            return f"pl_adxr(high, low, close, {args[0]})"
        if py_name == '__dmiplus__' and len(args) == 1:
            return f"pl_dmiplus(high, low, close, {args[0]})"
        if py_name == '__dmiminus__' and len(args) == 1:
            return f"pl_dmiminus(high, low, close, {args[0]})"
        # CountIf: count where condition is true over window
        if py_name == '__countif__':
            return f"pl_countif_window(close, open, {args[1]})"
        # Stochastic: multi-output var-parameter
        if py_name == '__stochastic__':
            # Stochastic(High, Low, Close, Length, SmoothK, SmoothD, SmoothType, oFastK, oFastD, oSlowK, oSlowD)
            # The output vars (oFastK, oFastD, oSlowK, oSlowD) are set by the function
            return f"pl_stochastic({', '.join(args)})"
        if py_name == '__fastk__' and len(args) == 1:
            return f"pl_fast_k(high, low, close, {args[0]})"
        if py_name == '__fastd__' and len(args) == 2:
            return f"pl_fast_d(high, low, close, {args[0]}, {args[1]})"
        if py_name == '__slowk__' and len(args) == 2:
            return f"pl_slow_k(high, low, close, {args[0]}, {args[1]})"
        if py_name == '__slowd__' and len(args) == 3:
            return f"pl_slow_d(high, low, close, {args[0]}, {args[1]}, {args[2]})"

        # Math functions
        if py_name == '__ceiling__' and len(args) == 1:
            self.has_math = True
            return f"math.ceil({args[0]})"
        if py_name == '__floor__' and len(args) == 1:
            self.has_math = True
            return f"math.floor({args[0]})"
        if py_name == '__round__' and len(args) == 1:
            self._need_f32_import = True
            return f"el_round0({args[0]})"
        if py_name == '__round__' and len(args) == 2:
            self._need_f32_import = True
            return f"el_round({args[0]}, {args[1]})"
        if py_name == '__expvalue__' and len(args) == 1:
            self.has_math = True
            return f"math.exp({args[0]})"

        # String functions
        if py_name == '__leftstr__' and len(args) == 2:
            return f"{args[0]}[:{args[1]}]"
        if py_name == '__rightstr__' and len(args) == 2:
            return f"{args[0]}[-{args[1]}:]"
        if py_name == '__midstr__' and len(args) == 3:
            # EL is 1-indexed: MidStr("abcdef",2,3)="bcd" → Python "abcdef"[1:4]
            return f"{args[0]}[({args[1]} - 1):({args[1]} - 1) + {args[2]}]"
        if py_name == '__strlen__' and len(args) == 1:
            return f"__builtin_len({args[0]})"
        if py_name == '__strcontains__' and len(args) == 2:
            return f"({args[1]} in {args[0]})"
        if py_name == '__strreplace__' and len(args) == 3:
            return f"{args[0]}.replace({args[1]}, {args[2]})"
        if py_name == '__strfind__' and len(args) == 2:
            return f"{args[0]}.find({args[1]})"
        if py_name == '__lowerstr__' and len(args) == 1:
            return f"{args[0]}.lower()"
        if py_name == '__upperstr__' and len(args) == 1:
            return f"{args[0]}.upper()"
        if py_name == '__stringformat__' and len(args) >= 1:
            fmt_args = ', '.join(args[1:])
            return f"{args[0]}.format({fmt_args})"

        # Date/time functions
        if py_name == '__hour__' and len(args) == 1:
            return f"({args[0]} // 100)"
        if py_name == '__minute__' and len(args) == 1:
            return f"({args[0]} % 100)"
        if py_name == '__second__' and len(args) == 1:
            return f"({args[0]} % 100)"

        # Output functions
        if py_name == '__plotpb__' and len(args) == 4:
            return f"_plots.setdefault('plotpb', []).append(({args[0]}, {args[1]}, {args[2]}, {args[3]}))"
        if py_name == '__printlog__':
            return f"print({', '.join(args)})"
        if py_name == '__setplotcolor__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_color', {args[1]})"
        if py_name == '__setplotwidth__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_width', {args[1]})"
        if py_name == '__setplotstyle__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_style', {args[1]})"

        # MC extensions
        if py_name == '__basedatanumber__':
            return "kwargs.get('base_data_number', 1)"
        if py_name == '__currentdatanumber__':
            return "kwargs.get('current_data_number', 1)"
        if py_name == '__recalculate__':
            return "kwargs.__setitem__('_needs_recalc', True)"
        if py_name == '__setstopposition__' and len(args) == 1:
            return f"_risk.__setitem__('stop_position', {args[0]})"
        if py_name == '__barnumberofdata__' and len(args) == 1:
            return f"kwargs.get('bar_number_of_data_{{{args[0]}}}', 0)"

        # Date/time no-arg functions
        if py_name == '__currentdate__':
            return "kwargs.get('current_date', 0)"
        if py_name == '__currenttime__':
            return "kwargs.get('current_time', 0)"
        if py_name == '__timetostring__' and len(args) == 1:
            return f"pl_timetostring({args[0]})"
        if py_name == '__datetostring__' and len(args) == 1:
            return f"pl_datetostring({args[0]})"
        if py_name == '__eldatetodatetime__' and len(args) == 1:
            # EL ELDateToDateTime(ELDate): convert an EL date (YYYMMDD,
            # years-since-1900) to a MC DateTime serial (days since 1899-12-30,
            # Excel-style). Identical day-serial base as DateToJulian.
            return f"pl_datetojulian({args[0]})"
        if py_name == '__eldatetodatetime__' and len(args) == 2:
            return f"({args[0]} * 10000 + {args[1]})"
        if py_name == '__eltimetodatetime__' and len(args) == 1:
            # ELTimeToDateTime(HHmm) -> time-of-day fraction (pdf:4499
            # ELTimeToDateTime(1015)=0.42708333).
            return f"(((int({args[0]}) // 100) * 3600 + (int({args[0]}) % 100) * 60) / 86400.0)"
        if py_name == '__datetimetoeldate__' and len(args) == 1:
            return f"int({args[0]} // 10000)"
        if py_name == '__datetimetoeltime__' and len(args) == 1:
            return f"int({args[0]} % 10000)"

        # Period data functions (OpenD, HighD, etc.)
        if py_name == '__period_data__':
            offset = int(args[0]) if args else 0
            return f"kwargs.get('{name}_{offset}', 0)"

        # Output extensions
        if py_name == '__setplotbgcolor__' and len(args) == 2:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_bgcolor', {args[1]})"
        if py_name == '__noplot__' and len(args) == 1:
            return f"_plots.__setitem__(f'plot{{{args[0]}}}_noplot', True)"

        # Order extensions
        if py_name == '__setbreakeven__' and len(args) == 1:
            return f"_risk.__setitem__('breakeven', {args[0]})"
        if py_name == '__entryname__' and len(args) >= 1:
            return "kwargs.get('entry_name', '')"
        if py_name == '__exitname__' and len(args) >= 1:
            return "kwargs.get('exit_name', '')"

        # CurrentSession: allow runner to override via kwarg (runner computes from bar date/time).
        if py_name == 'pl_currentsession':
            return f"kwargs.get('currentsession', pl_currentsession({', '.join(args)}))"

        # SessionLastBar: runner derives "last bar of the trading session" from the bar
        # date series (next bar is a new day) and supplies it via 'session_last_bar',
        # mirroring CurrentSession. Not capture-fed — derived from the date series.
        if py_name == 'pl_sessionlastbar':
            return f"kwargs.get('session_last_bar', pl_sessionlastbar({', '.join(args)}))"

        # LastCalc*: date/time of the FINAL bar of the data series (the moment of the
        # last calculation in a historical export). The runner derives these from the
        # last bar's date/time series (fundamental inputs, NOT the lc* output columns),
        # mirroring CurrentSession/SessionLastBar. Constant across all bars.
        _LASTCALC_KW = {
            'pl_lastcalcdatetime': 'lastcalcdatetime',
            'pl_lastcalcjdate': 'lastcalcjdate',
            'pl_lastcalcmmtime': 'lastcalcmmtime',
            'pl_lastcalcmstime': 'lastcalcmstime',
            'pl_lastcalcsstime': 'lastcalcsstime',
        }
        if py_name in _LASTCALC_KW:
            return f"kwargs.get('{_LASTCALC_KW[py_name]}', {py_name}({', '.join(args)}))"

        # Removed the _KWARGS_PREFER hook for session/config funcs — it
        # existed only to consume capture-fed kwargs (circular self-feed). Session/contract-spec
        # values must come from honest runtime impls or known chart config, not the capture.
        if py_name and not py_name.startswith('__'):
            return f"{py_name}({', '.join(args)})"

        if py_name:
            # A BUILTIN_FUNC_MAP keyword whose internal `__marker__` py_name had no
            # faithful inline expansion here (e.g. wrong arg count, or a bare
            # zero-arg reference like `Value1 = FastD;`). Emitting the marker
            # (`__fastd__()`) would be an undefined name — strict returns broken
            # output and partial stamps a runnable header on unrunnable code. Route
            # through the unimplemented path instead (recording the EL name, not the
            # marker): strict collects+raises, partial emits an argless stub.
            return self._impl_call(name, node, args)

        # Unknown function: use pl_ prefix (validated to be implemented in builtins)
        return self._impl_call(name, node, args)

    def _gen_bar_ref(self, node: Node) -> str:
        # User variables access _state history list; data series access the list directly.
        # EL: VarName[n] = value n bars ago. User var state list stores end-of-bar values,
        # so VarName[n] = _state['name'][-n] (not -(n+1) like data series).
        series = node['series']
        index = node['index']

        # Input[n]: an input is constant for the run, so its value n bars ago is
        # its value. Inputs are bound as scalars; subscripting them would raise.
        # The index is still walked so a nested unimplemented keyword is caught.
        if (isinstance(series, dict) and series.get('type') == 'ident'
                and series.get('name') in {n for n, _ in self._input_inits}):
            self.gen_expr(index)
            input_name: str = series['name']
            return input_name

        # Position-info function call: MaxPositionProfit(1), MaxContracts(1), etc.
        # The parser models these as bar_ref(series=position_kw, index=PosAgo).
        # The runtime supplies them as a SCALAR kwarg (no positions-ago history),
        # so subscripting it (`kwargs.get(...)[-2]`) raises 'int not subscriptable'
        # and aborts the whole bar — zeroing every traced column. Return the
        # scalar value directly, discarding the positions-ago index.
        #
        # The positions-ago INDEX must still be WALKED so a nested
        # unimplemented keyword inside it is recorded (e.g. MaxContracts(FakeF(Close))
        # / EntryPrice(FakeF(Close))); otherwise the unknown would be silently
        # accepted (strict would emit kwargs.get(...) and partial would report
        # '0 unimplemented constructs' — a watermark lie). Its
        # emitted value is discarded (the kwarg is a scalar with no history), but
        # its subtree is scanned exactly like any other arg; in partial mode, if the
        # index collected an unimplemented entry, emit the argless stub so it raises.
        #
        # Exception: EntryDate/EntryTime/EntryPrice/ExitDate/ExitTime/ExitPrice(n)
        # and MaxShares(n) DO have positions-ago history (the closed-position
        # ledger), so they emit
        # pl_pos_ago(kwargs, kwarg, n) instead of discarding n (issue #9).
        if series['type'] == 'position_kw':
            before = len(self._unimpl_errors)
            idx_expr = self.gen_expr(index)
            if self._partial and len(self._unimpl_errors) > before:
                e = self._unimpl_errors[before]
                return f"pl_partial_stub({e['name']!r}, {e['line']!r})()"
            if series['kwarg'] in _POS_AGO_KWARGS:
                return f"pl_pos_ago(kwargs, '{series['kwarg']}', {idx_expr})"
            return self.gen_expr(series)

        if series['type'] == 'ident' and series['name'] in self._stateful_vars and self.persist:
            name: str = series['name']
            if index['type'] == 'number':
                n = int(index['value'])
                if n == 0:
                    return name  # VarName[0] = current value
                # Safe access: if not enough history, return first (initial) value
                # Use __builtin_len because 'len' may be shadowed by an EL input
                return f"(_state['{name}'][-{n}] if __builtin_len(_state['{name}']) >= {n} else _state['{name}'][0])"
            idx_expr = self.gen_expr(index)
            idx_int = f"int({idx_expr})"
            return f"(_state['{name}'][-{idx_int}] if __builtin_len(_state['{name}']) >= {idx_expr} else _state['{name}'][0])"

        # Function-call historical reference: Highest(High, Len)[1]
        if series['type'] == 'call':
            # For func(args...)[n], compute the function on data from n bars ago
            # by slicing the data series arguments.
            func_name = series['name']
            old_ctx = self._series_context
            if func_name in self._SERIES_FUNCS:
                self._series_context = True
            raw_args = [self.gen_expr(a) for a in series['args']]
            self._series_context = old_ctx

            if index['type'] == 'number':
                offset: Union[int, str] = int(index['value'])  # EL 1-indexed → remove N trailing elements
            else:
                offset_expr = self.gen_expr(index)
                offset = f"({offset_expr})"

            # Inject high/low/close for indicators that need them (__adx__, __cci__, etc.)
            py_name = BUILTIN_FUNC_MAP.get(func_name)
            if py_name in ('__adx__', '__adxr__', '__dmiplus__', '__dmiminus__') and len(raw_args) == 1:
                fn = {'__adx__': 'pl_adx', '__adxr__': 'pl_adxr',
                      '__dmiplus__': 'pl_dmiplus', '__dmiminus__': 'pl_dmiminus'}[py_name]
                hlc = ', '.join(self._lagged(s, offset, slice_zero=True)
                                for s in ('high', 'low', 'close'))
                return f"{fn}({hlc}, {raw_args[0]})"

            # For regular functions, pass series args with offset slicing
            # Slice each data series argument to shift the lookback window
            sliced_args = []
            site = self._price_arg_site(series)
            for i, arg in enumerate(series['args']):
                if i == 0 and site is not None:
                    sliced_args.append(self._lagged(site, offset))
                elif arg['type'] == 'ident' and arg['name'] in _SERIES_PARAMS:
                    sliced_args.append(self._lagged(raw_args[i], offset))
                else:
                    sliced_args.append(raw_args[i])

            if py_name and not py_name.startswith('__'):
                return f"{py_name}({', '.join(sliced_args)})"
            return self._impl_call(func_name, series, sliced_args)

        # Range[n] / TrueRange[n]: the value n bars ago, built from the data
        # columns n (and n + 1) bars ago like the EL functions themselves.
        if (series['type'] == 'ident' and series['name'] in ('range', 'truerange')
                and not self._is_user_name(series['name'])):
            return self._gen_lagged_range(series['name'], index)

        # Original behaviour for data series and arrays
        old_ctx = self._series_context
        self._series_context = True
        series_expr = self.gen_expr(series)
        self._series_context = old_ctx

        if index['type'] == 'number':
            n_str = index['value']
            n = int(n_str)
            if n > 0 and series_expr in ('close', 'high', 'low', 'open', 'volume', 'ticks', 'upticks', 'downticks'):
                # EL: on first bar(s), historical refs fall back to current bar's Open
                # Runner pads 1 element; check if index hits padding region
                return f"({series_expr}[-({n_str} + 1)] if __builtin_len({series_expr}) > 1 + {n} else open[-1])"
            if n > 0 and series_expr in ('date', 'time'):
                # Past the start of the tape Date/Time read 0, like the
                # leading pad (Date[1] on bar 1) and the native kernel.
                return f"({series_expr}[-({n_str} + 1)] if __builtin_len({series_expr}) > {n} else 0)"
            return f"{series_expr}[-({n_str} + 1)]"

        idx_expr = self.gen_expr(index)
        idx_int = f"int({idx_expr})"
        if series_expr in ('close', 'high', 'low', 'open', 'volume', 'ticks', 'upticks', 'downticks'):
            return f"({series_expr}[-({idx_int} + 1)] if __builtin_len({series_expr}) > 1 + ({idx_expr}) else open[-1])"
        if series_expr in ('date', 'time'):
            return f"({series_expr}[-({idx_int} + 1)] if __builtin_len({series_expr}) > {idx_int} else 0)"
        return f"{series_expr}[-({idx_int} + 1)]"
