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

"""pl_transpiler/runtime/accel.py — the Python-side caller for the native kernel.

The Rust cdylib (``pl_transpiler._accel``) exposes a single strategy-bearing
FFI crossing, ``run_program(program_ir, series, data2, config)`` (ACCEL
interface v2). This module is the *caller* that owns that crossing: it turns
EL source + bars + instrument config into the four FFI arguments, makes the one
call, and interprets the result.

The caller owns the fallback decision — the kernel never guesses. If the
extension cannot be imported, or the kernel refuses the program, this module
signals that *loudly* so the caller never receives a partial or approximate
trade list; it is the caller's job to fall back to the Python path and to do so
visibly.

Public surface:

    run_native(el_source, bars, config, *, data2_bars=None) -> list[dict]
        EL source -> native fills (``FillEngine.fills`` / ``get_mc_trades``
        shape, minus ``order_no``). Exactly one FFI crossing.

    run_native_inputs(el_source, bars, config, inputs, *, data2_bars=None) -> list[dict]
        The same run with explicit Input overrides — the sweep's "solo"
        reference that a batch must reproduce bit-for-bit.

    run_native_sweep(el_source, bars, config, variants, *, data2_bars=None,
                     threads=None) -> list[list[dict]]
        K input variants over ONE tape, compiled and marshalled once. With
        ``threads=None`` (default) it returns the sequential batch; ``threads=N``
        fans the sweep across N worker threads (released GIL). ``out[i]`` is
        bit-identical to ``run_native_inputs`` with ``variants[i]``.

    run_native_audit_packet(el_source, bars, config, *, data2_bars=None,
                            hash_tapes=False) -> bytes
        Same production loop as ``run_native``, returning one framed
        ``audit-event-v1`` packet. Not a replacement for ``run_program``.

    audit_skip_eval_from(el_source, dates, inputs=None) -> (eval_from, proven, reason)
        Prove the prefix skip from IR + the Data1 date column alone (no OHLC
        marshal). The slice-before-marshal caller uses ``eval_from`` as the
        slice point so the skip-region bars never cross the FFI.

    run_native_audit_packet_inputs(el_source, bars, config, inputs, *, data2_bars=None,
                                   eval_from=0, source_bar_count=None) -> bytes
        One packet with explicit ``inputs`` — the sweep's solo reference. With
        explicit ``eval_from``/``source_bar_count`` (and a sliced ``bars``) it is
        byte-identical to ``run_native_audit_packet`` over the full tape.

    run_native_audit_packet_batch(el_source, bars, config, variants, *, data2_bars=None,
                                  eval_from=0, source_bar_count=None) -> list[bytes]
        K packets over ONE tape, compiled + marshalled once. ``out[i]`` is
        byte-identical to ``run_native_audit_packet_inputs`` with ``variants[i]``.

    run_native_audit_packet_sweep(el_source, bars, config, variants, *, data2_bars=None)
            -> (packets, eval_from, source_bar_count, skip_reason)
        Slice-before-marshal sweep: prove the skip, slice the columns to
        ``[eval_from:]`` BEFORE boxing them across the FFI, marshal once, emit
        K packets. The single-process K-config path.
    run_native_trace(el_source, bars, config, *, data2_bars=None)
        -> (columns, rows, fills)
        The typed per-bar trace sink: one FFI crossing emits the
        kernel's register values typed (float/int/bool/str) instead of the
        pre-formatted 24-column text, so Python owns ``str(value)`` rendering.
        Returns the sorted columns, one typed row per evaluated bar, and the
        same ordered fills as ``run_native`` (its trade-list digest must equal
        ``pl_oracle``).

    marshalled_series(bars, data2_bars=None) -> (series, data2)
        The stable FFI ``series`` / ``data2`` dicts, cached by tape identity
        so a sweep of N variants over one tape marshals once. Sweep runners
        hoist this out of the per-variant loop; ``run_native`` uses it too.

    marshal_count() -> int
        Process-wide count of tapes actually boxed into Python lists. A
        cache hit (same tape object reused) does not increment this.

    accel_available() -> bool
        True iff ``import pl_transpiler._accel`` succeeds.

    native_status() -> (bool, str)
        Whether the kernel is usable, plus one line saying so (its version) or
        why not; ``pl_oracle --version`` / ``pl_run --version`` print the line.

    AccelUnavailable / AccelRefused
        Raised by ``run_native`` when the extension is absent, or when the
        kernel refuses the program (NotImplementedError). Both are LOUD
        signals to fall back to the Python path — never a result.

    explain_native(el_source) -> list[NativeRefusal]
        Every construct the native compile pass would refuse, not just the
        first: ``(kind, name, line, message)`` records in compile order.
        Empty means native can run the program. Raises ``AccelUnavailable``
        when the extension is absent (never a guess from Python).

    fallback_count() -> int
        Process-wide count of native falls back (AccelUnavailable or
        AccelRefused signalled by ``run_native``). The differential gate
        asserts this is unchanged (zero) around a native run so a digest can
        never come from a silent Python fallback — that would be a false green.
"""
import json
from collections import OrderedDict, namedtuple
from typing import TYPE_CHECKING, Any, Optional, Union, cast

from pl_transpiler.ir import serialise_ir
from pl_transpiler.parser import parse_source
from pl_transpiler.runtime.bar_tape import ohlcv_columns

if TYPE_CHECKING:
    from pl_transpiler.runtime.instrument_config import InstrumentConfig

Fill = dict[str, Any]
Series = dict[str, list[Any]]

# The FFI config requires a session `end` and the v2-required commission /
# slippage / MaxBarsBack / warmup-seed fields, none of which the Python
# ``InstrumentConfig`` carries. The native fill engine consumes only
# ``bigpointvalue`` and ``tick`` (the ``Config -> Instrument`` mapping), so the
# remaining fields are supplied as the documented NQ/ES defaults and do not
# affect the trade list. ``session.end`` is the @NQ#C / @ES#C session close.
_SESSION_END = 1515

# Process-wide fallback counter. ``run_native`` never falls back silently; the
# two loud signals below (absent extension, refused program) each bump this
# counter when raised, and the differential gate asserts it is unchanged around
# a native run. Kept as an explicit counter rather than a bool so the gate reads
# "fallback flag must be zero", not "no exception happened to escape a try".
_fallback_count = 0


def _record_fallback() -> None:
    global _fallback_count
    _fallback_count += 1


def fallback_count() -> int:
    """Process-wide number of native-path fallbacks signalled so far.

    Incremented only when ``run_native`` raises ``AccelUnavailable`` or
    ``AccelRefused``. A successful native run never increments it, so a gate can
    assert it is unchanged around a run to prove the native path was taken.
    """
    return _fallback_count


class AccelUnavailable(RuntimeError):
    """``pl_transpiler._accel`` cannot be imported; the Python path must run."""


class AccelRefused(RuntimeError):
    """The native kernel refused to execute the program (unsupported surface).

    Carries the kernel's own refusal message. This is never a partial or
    approximate trade list — the caller must fall back to Python loudly.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_ACCEL_MISSING_WARNED = False


def warn_python_path(reason: Optional[str] = None) -> None:
    """One short stderr line (once per process) that the slower Python path ran.

    ``reason=None`` means the Rust module is not installed.
    For CLI entry points only; library code logs :func:`python_path_note`
    instead so importing callers never get unsolicited stderr output.
    """
    global _ACCEL_MISSING_WARNED
    if _ACCEL_MISSING_WARNED:
        return
    _ACCEL_MISSING_WARNED = True
    import sys
    sys.stderr.write(python_path_note(reason) + "\n")


def python_path_note(reason: Optional[str] = None) -> str:
    """The one-line note that the slower Python path ran (no trailing newline).

    ``reason=None`` means the Rust module is not installed.
    """
    if reason is None:
        return ("note: powerbridge-accel not installed; using the pure-Python "
                "engine (slower). See "
                "https://github.com/hudsonwa/PowerBridge/blob/main/docs/ACCEL.md#install")
    return "note: using the pure-Python engine (slower): %s" % reason


_REFUSED = "native path refused: "
_UNSUPPORTED = "unsupported native construct: "


def refusal_note(tool: str, reason: str) -> str:
    """The one-line stderr note for a run the native kernel refused.

    ``reason`` is a ``native path refused: ...`` fallback reason. The line
    says the Python engine ran and why; :func:`refusal_detail` is the ``-v``
    line. No trailing newline.
    """
    detail = reason[len(_REFUSED):] if reason.startswith(_REFUSED) else reason
    if detail.startswith(_UNSUPPORTED):
        why = f"the native kernel does not support {detail[len(_UNSUPPORTED):]} yet"
    else:
        why = f"the native kernel cannot run it ({detail})"
    return (f"[{tool}] note: ran on the Python engine because {why}; "
            "results are the same, only slower (-v for details)")


def refusal_detail(tool: str, reason: str) -> str:
    """The ``-v`` line after :func:`refusal_note`: the kernel's own refusal."""
    return (f"[{tool}] {reason}; `pl_oracle --explain-native EL_FILE` lists "
            "every construct the native kernel refuses")


# Fill-log semantics this core implements (fill log v2; the oracle digest is
# ``pl_oracle.DIGEST_VERSION``). The kernel exports its own value as
# ``_accel.FILL_LOG_VERSION``; a kernel that is missing it or reports a
# different value is not used, so the native path can never return different
# fills from the Python path.
FILL_LOG_VERSION = 2


def _version_mismatch(accel: Any) -> Optional[str]:
    """None if the kernel's fill-log version matches, else the reason."""
    got = getattr(accel, "FILL_LOG_VERSION", None)
    if got == FILL_LOG_VERSION:
        return None
    kernel = getattr(accel, "__version__", None) or "(unknown version)"
    have = "fill-log v%s" % got if got is not None else "an older fill log (no FILL_LOG_VERSION)"
    return ("powerbridge-accel %s implements %s; this powerbridge needs fill-log v%d; "
            "using the Python path" % (kernel, have, FILL_LOG_VERSION))


def _probe() -> tuple[Any, Optional[str]]:
    """``(kernel, reason)``: the importable kernel, or None and why it is unusable."""
    try:
        import pl_transpiler._accel as accel
    except ModuleNotFoundError as exc:
        if exc.name in ("pl_transpiler._accel", "pl_transpiler"):
            return None, "not installed"
        return None, "import failed: %s" % exc
    except ImportError as exc:
        return None, "import failed: %s" % exc
    return accel, _version_mismatch(accel)


def accel_available() -> bool:
    """True iff the native extension imports and matches this core's fill log."""
    accel, reason = _probe()
    return accel is not None and reason is None


def native_status() -> tuple[bool, str]:
    """``(available, line)`` for the native kernel, the same check the run uses.

    ``line`` is e.g. ``"native kernel: powerbridge-accel 0.13.0 (available)"`` or
    ``"native kernel: not available (not installed); install ..."``.
    """
    accel, reason = _probe()
    if accel is not None and reason is None:
        kernel = getattr(accel, "__version__", None) or "(unknown version)"
        return True, "native kernel: powerbridge-accel %s (available)" % kernel
    reason = cast(str, reason).replace("; using the Python path", "")
    return False, ("native kernel: not available (%s); install the "
                   "powerbridge-accel wheel for your platform, see docs/ACCEL.md"
                   % " ".join(reason.split()))


def _import_accel() -> Any:
    try:
        import pl_transpiler._accel as accel
    except ImportError as exc:
        _record_fallback()
        raise AccelUnavailable(
            "pl_transpiler._accel is not installed (build it: see README "
            "'Acceleration', or tools/build_accel.sh); the Python "
            "path is the only path"
        ) from exc
    reason = _version_mismatch(accel)
    if reason is not None:
        _record_fallback()
        raise AccelUnavailable(reason)
    return accel


def _series_dict(bars: Any) -> Series:
    """The FFI ``series`` shape: plain lists of the seven bar columns."""
    cols = ohlcv_columns(bars)
    return {
        "open": list(cols["open"]),
        "high": list(cols["high"]),
        "low": list(cols["low"]),
        "close": list(cols["close"]),
        "volume": list(cols["volume"]),
        "date": [int(d) for d in cols["date"]],
        "time": [int(t) for t in cols["time"]],
    }


# Identity cache of marshalled series dicts. Keyed by id(tape) and verified
# with `is`, so a recycled CPython id never returns a stale dict. Config is
# never part of the key — a sweep of N variants over one tape marshals once.
# Bounded so a long process does not pin every tape it has ever seen.
_MAX_CACHED_TAPES = 4
_series_cache: "OrderedDict[int, tuple[Any, Series]]" = OrderedDict()  # id(bars) -> (bars, series_dict)
_marshal_count = 0


def marshal_count() -> int:
    """Number of times a tape has been boxed into the FFI series lists.

    A cache hit (same tape object reused) does not increment this. Tests and
    sweep runners use it to prove a repeat run over the same tape did not
    re-marshal.
    """
    return _marshal_count


def clear_series_cache() -> None:
    """Drop cached series dicts. Tests use this to start from a known count."""
    _series_cache.clear()


def _cached_series_dict(bars: Any) -> Optional[Series]:
    """Return ``_series_dict(bars)``, hitting the identity cache when possible."""
    global _marshal_count
    if bars is None:
        return None
    key = id(bars)
    entry = _series_cache.get(key)
    if entry is not None and entry[0] is bars:
        _series_cache.move_to_end(key)
        return entry[1]
    _marshal_count += 1
    series = _series_dict(bars)
    _series_cache[key] = (bars, series)
    _series_cache.move_to_end(key)
    while len(_series_cache) > _MAX_CACHED_TAPES:
        _series_cache.popitem(last=False)
    return series


def marshalled_series(bars: Any, data2_bars: Any = None
                      ) -> tuple[Optional[Series], Optional[Series]]:
    """Return the FFI ``(series, data2)`` mappings, cached by tape identity.

    Keyed on the identity of ``bars`` and ``data2_bars``, never on config.
    A different tape object is always a miss (never a stale dict). Sweep
    runners can call this once outside the per-variant loop and pass the
    dicts to the stable ``_accel.run_program``; ``run_native`` uses the
    same cache internally.

    The returned dicts are the stable ``series`` shape (seven plain lists).
    """
    series = _cached_series_dict(bars)
    data2 = _cached_series_dict(data2_bars) if data2_bars is not None else None
    return series, data2


def _config_dict(config: "InstrumentConfig", max_bars_back: Union[int, str] = 0,
                 max_entries_per_direction: Optional[int] = 0) -> dict[str, Any]:
    """InstrumentConfig -> the FFI ``config`` mapping (v2, all fields explicit)."""
    return {
        "name": config.name,
        "bigpointvalue": float(config.bigpointvalue),
        "pointvalue": float(config.pointvalue),
        "minmove": int(config.minmove),
        "pricescale": int(config.pricescale),
        "tick": float(config.tick),
        "barinterval": int(config.barinterval),
        "session": {
            "start": int(config.sess1starttime),
            "end": _SESSION_END,
        },
        "commission": 0.0,
        "slippage": 0.0,
        "max_bars_back": int(max_bars_back),
        "max_entries_per_direction": int(max_entries_per_direction or 0),
        "warmup_seed": 0,
        "symccy": int(config.symccy),
        "symnm": str(config.symnm),
        "symalt": str(config.symalt),
        "descstr": str(config.descstr),
        "expdate": int(config.expdate),
        "round_turn_cost": float(config.round_turn_cost),
    }


def run_native(el_source: str, bars: Any, config: "InstrumentConfig", *,
               data2_bars: Any = None, max_bars_back: Union[int, str] = 0,
               max_entries_per_direction: Optional[int] = 0,
               prints: Optional[list[str]] = None) -> list[Fill]:
    """Execute ``el_source`` over ``bars`` through the native kernel.

    Exactly one FFI crossing (``run_program``): serialise the EL to ``program_ir``,
    build the ``series``/``config`` mappings, and return the ordered fills.

    ``bars`` are ``load_bars_csv`` / ``load_bars`` style bar dicts, or a
    ``BarTape``. ``config`` is an ``InstrumentConfig``. ``data2_bars``, when the
    program references Data2, is a full second bar series (same shape as
    ``bars``); the kernel aligns it to the primary tape by ``(date, time)``.

    Returns the ordered fills list in ``FillEngine.fills`` / ``get_mc_trades``
    shape (without the ``order_no`` key — that is Python-side bookkeeping).

    Raises ``AccelUnavailable`` when the extension is not built, and
    ``AccelRefused`` when the kernel refuses the program (a construct outside
    the implemented surface). Neither is a trade list — the caller must fall
    back to Python loudly.

    ``max_bars_back`` (default 0) skips the first N bars as history only. A
    kernel built before MaxBarsBack support ignores the config field, so for
    N > 0 such a kernel is refused (``AccelRefused``) rather than trusted.
    ``max_entries_per_direction`` (default 0 = unlimited) caps same-direction
    entries; an older kernel without that support is refused the same way.

    ``prints``, when given a list, receives the run's ``Print`` lines (the
    same lines as the Python path's ``pl_print``). Recording them costs a
    line per ``Print`` per bar and the prefix skip, so leave it ``None``
    unless the lines are wanted; a kernel built before Print output is then
    refused only when they are.
    """
    accel = _import_accel()
    if max_bars_back and not getattr(accel, "MAX_BARS_BACK", False):
        _record_fallback()
        raise AccelRefused(
            "this accel build predates max_bars_back; rebuild accel "
            "(tools/build_accel.sh) to run it natively")
    if max_entries_per_direction and not getattr(accel, "MAX_ENTRIES_PER_DIRECTION", False):
        _record_fallback()
        raise AccelRefused(
            "this accel build predates max_entries_per_direction; rebuild accel "
            "(tools/build_accel.sh) to run it natively")
    ast = parse_source(el_source)
    program_ir = serialise_ir(ast)
    series, data2 = marshalled_series(bars, data2_bars)
    config_map = _config_dict(config, max_bars_back, max_entries_per_direction)
    try:
        return cast("list[Fill]", _call_with_prints(
            accel, "run_program", ast, prints, program_ir, series, data2, config_map))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def _has_print(node: Any) -> bool:
    """Whether the parsed program has a ``Print`` statement anywhere."""
    if isinstance(node, dict):
        return node.get("type") == "print" or any(_has_print(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_print(v) for v in node)
    return False


def _call_with_prints(accel: Any, name: str, ast: Any,
                      prints: Optional[list[str]], *args: Any) -> Any:
    """``accel.<name>(*args)``. For a program that prints, given a ``prints``
    list, the ``<name>_prints`` variant instead, whose last result (the Print
    lines) is appended to ``prints``.

    A kernel built before Print output is refused for a program that prints,
    so its lines are never silently lost.
    """
    if prints is None or not _has_print(ast):
        return getattr(accel, name)(*args)
    with_prints = getattr(accel, name + "_prints", None)
    if with_prints is None:
        _record_fallback()
        raise AccelRefused(
            "this accel build predates Print output; rebuild accel "
            "(tools/build_accel.sh) to run it natively")
    *result, lines = with_prints(*args)
    prints.extend(lines)
    return result[0] if len(result) == 1 else tuple(result)


NativeRefusal = namedtuple("NativeRefusal", "kind name line message")
NativeRefusal.__doc__ = """One construct the native kernel refuses.

``kind`` is ``function``, ``statement``, ``order``, ``variable`` or
``expression``; ``name`` the keyword or construct; ``line`` the 1-based EL
source line of the enclosing statement (0 if unknown); ``message`` the
kernel's refusal text, as ``AccelRefused`` would carry it."""


def explain_native(el_source: str) -> "list[NativeRefusal]":
    """List every construct the native kernel would refuse in ``el_source``.

    Returns a list of :class:`NativeRefusal` in the kernel's compile order;
    the first one's ``message`` is the refusal :func:`run_native` raises. An
    empty list means the compile pass accepts the program. Only the compile
    pass is checked: run-time conditions (a missing Data2 tape, MaxBarsBack on
    an entry point without it) are not listed. A refused statement is skipped,
    so a second refusal inside the same expression is reported on the next
    run once the first is fixed.

    Nothing is executed and the fallback counter is not touched. Raises
    ``AccelUnavailable`` when the extension is not built (or too old to
    explain), and the parser's ``PLSyntaxError`` for invalid EL.
    """
    try:
        import pl_transpiler._accel as accel
    except ImportError as exc:
        raise AccelUnavailable(
            "native kernel not installed: pl_transpiler._accel is not built "
            "(see README 'Acceleration', or tools/build_accel.sh)") from exc
    if not hasattr(accel, "explain_program"):
        raise AccelUnavailable(
            "this accel build predates explain_native; rebuild accel "
            "(tools/build_accel.sh)")
    ast = parse_source(el_source)
    try:
        refusals = accel.explain_program(serialise_ir(ast))
    except BaseException as exc:
        # pyo3 raises a kernel panic as PanicException (a BaseException).
        if type(exc).__name__ != "PanicException":
            raise
        raise RuntimeError(
            f"native kernel crashed while explaining this program ({exc}); "
            "please report it with the EL source") from exc
    # The kernel names keywords by their parser-internal kwarg (entry_time);
    # report the EL keyword (EntryTime) instead.
    from pl_transpiler.codegen_el import el_keyword
    return [NativeRefusal(kind, (kind == "expression" and el_keyword(name)) or name,
                          line, message)
            for kind, name, line, message in refusals]


def run_native_drawings(el_source: str, bars: Any, config: "InstrumentConfig", *,
                        data2_bars: Any = None, max_bars_back: Union[int, str] = 0,
                        max_entries_per_direction: Optional[int] = 0,
                        prints: Optional[list[str]] = None
                        ) -> tuple[list[Fill], list[Any]]:
    """Native fills plus the drawing objects that run recorded.

    Returns ``(fills, objects)``. ``objects`` is the drawing-dump object list
    (``seq`` still 0). The caller builds the v1 envelope. Same refusal rules
    as :func:`run_native`, and ``prints`` as there. A kernel built before
    drawing support is refused.
    """
    accel = _import_accel()
    if not hasattr(accel, "run_program_drawings"):
        _record_fallback()
        raise AccelRefused(
            "this accel build does not record drawings; rebuild accel "
            "(tools/build_accel.sh)")
    if max_bars_back and not getattr(accel, "MAX_BARS_BACK", False):
        _record_fallback()
        raise AccelRefused(
            "this accel build predates max_bars_back; rebuild accel "
            "(tools/build_accel.sh) to run it natively")
    if max_entries_per_direction and not getattr(accel, "MAX_ENTRIES_PER_DIRECTION", False):
        _record_fallback()
        raise AccelRefused(
            "this accel build predates max_entries_per_direction; rebuild accel "
            "(tools/build_accel.sh) to run it natively")
    ast = parse_source(el_source)
    program_ir = serialise_ir(ast)
    series, data2 = marshalled_series(bars, data2_bars)
    config_map = _config_dict(config, max_bars_back, max_entries_per_direction)
    try:
        fills, raw = _call_with_prints(
            accel, "run_program_drawings", ast, prints,
            program_ir, series, data2, config_map)
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc
    return fills, json.loads(raw)


def run_native_trace(el_source: str, bars: Any, config: "InstrumentConfig", *,
                     data2_bars: Any = None, max_bars_back: Union[int, str] = 0,
                     max_entries_per_direction: Optional[int] = 0,
                     prints: Optional[list[str]] = None
                     ) -> tuple[list[str], list[list[Any]], list[Fill]]:
    """Run ``el_source`` natively and return the typed per-bar trace + fills.

    The typed trace sink: one FFI crossing emits the kernel's
    register values **typed** (``float`` / ``int`` / ``bool`` / ``str``), so
    text rendering (``str(value)``) is the caller's — the reverse of the
    pre-formatted 24-column text sink. Returns ``(columns, rows, fills)``:

    * ``columns`` — the sorted union of bar metadata, engine position columns
      and the strategy's variables and inputs (``run_el``'s columns);
    * ``rows`` — one typed list per evaluated bar, aligned to ``columns``;
    * ``fills`` — the ordered ``FillEngine.fills`` list, identical to
      ``run_native``'s (the trade-list digest of a trace run must equal
      ``pl_oracle`` bit-for-bit).

    ``str()`` of every cell equals ``run_el``'s cell. A program whose
    variable types (int, float, bool or str) depend on the run is refused.

    ``max_bars_back`` / ``max_entries_per_direction`` behave as in
    :func:`run_native`: the first N bars are history only (no row; the
    ``_barnumber`` column starts at 1 after them). A kernel whose trace entry
    predates them is refused for a non-zero value. ``prints`` is as in
    :func:`run_native`.

    Raises ``AccelUnavailable`` / ``AccelRefused`` — never a partial trace.
    """
    accel = _import_accel()
    ast = parse_source(el_source)
    program_ir = serialise_ir(ast)
    series, data2 = marshalled_series(bars, data2_bars)
    if ((max_bars_back or max_entries_per_direction)
            and not getattr(accel, "TRACE_RUN_OPTIONS", False)):
        _record_fallback()
        raise AccelRefused(
            "this accel build's trace predates max_bars_back / "
            "max_entries_per_direction; rebuild accel (tools/build_accel.sh)")
    config_map = _config_dict(config, max_bars_back, max_entries_per_direction)
    try:
        return cast("tuple[list[str], list[list[Any]], list[Fill]]", _call_with_prints(
            accel, "run_program_trace", ast, prints,
            program_ir, series, data2, config_map))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def _serialise_and_marshal(el_source: str, bars: Any, data2_bars: Any
                           ) -> tuple[Any, str, Optional[Series], Optional[Series]]:
    """The three FFI pre-amble steps shared by every native entry point:
    EL -> program_ir, bars -> cached series dicts, config -> FFI mapping."""
    accel = _import_accel()
    ast = parse_source(el_source)
    program_ir = serialise_ir(ast)
    series, data2 = marshalled_series(bars, data2_bars)
    return accel, program_ir, series, data2


def run_native_inputs(el_source: str, bars: Any, config: "InstrumentConfig",
                      inputs: dict[str, Any], *, data2_bars: Any = None) -> list[Fill]:
    """Run ``el_source`` over ``bars`` through the native kernel with explicit
    Input overrides (a mapping of input name -> number/bool/string).

    The sweep's "solo" reference: one variant run in isolation. The batch
    path must reproduce this trade list bit-for-bit.
    """
    accel, program_ir, series, data2 = _serialise_and_marshal(
        el_source, bars, data2_bars
    )
    config_map = _config_dict(config)
    try:
        return cast("list[Fill]", accel.run_program_inputs(program_ir, series, data2, config_map, inputs))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def run_native_sweep(el_source: str, bars: Any, config: "InstrumentConfig",
                     variants: list[dict[str, Any]], *, data2_bars: Any = None,
                     threads: Optional[int] = None) -> list[list[Fill]]:
    """Run K input variants over ONE tape, compiled and marshalled once.

    ``variants`` is a list of input-override dicts (name -> number/bool/string).
    ``out[i]`` is bit-identical to ``run_native_inputs(el_source, …, variants[i])``
    — the solo_digest == batch_digest invariant a sweep verifies.

    ``threads`` is ``None`` for the sequential batch (compile/lower/tape-marshal
    happen once instead of K times) or an int worker count to fan the sweep
    across cores (the released-GIL, embarrassingly-parallel path).
    """
    accel, program_ir, series, data2 = _serialise_and_marshal(
        el_source, bars, data2_bars
    )
    config_map = _config_dict(config)
    try:
        if threads is None:
            return cast("list[list[Fill]]", accel.run_program_batch(program_ir, series, data2, config_map, variants))
        return cast("list[list[Fill]]", accel.run_program_batch_parallel(
            program_ir, series, data2, config_map, variants, threads
        ))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def run_native_audit_packet(el_source: str, bars: Any, config: "InstrumentConfig", *,
                            data2_bars: Any = None, hash_tapes: bool = False) -> bytes:
    """Production native loop returning one framed audit-event-v1 packet.

    Same IR/tape/config marshalling as ``run_native``. Does not replace
    ``run_program``; this is a new FFI entry point. Returns ``bytes``.
    """
    accel, program_ir, series, data2 = _serialise_and_marshal(
        el_source, bars, data2_bars
    )
    config_map = _config_dict(config)
    try:
        return cast("bytes", accel.run_program_audit_packet(
            program_ir, series, data2, config_map, hash_tapes
        ))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def audit_skip_eval_from(el_source: str, dates: Any, inputs: Optional[dict[str, Any]] = None
                         ) -> tuple[int, bool, str]:
    """Prove the prefix skip from IR + the Data1 date column alone (no OHLC).

    Returns ``(eval_from, proven, reason)``. The slice-before-marshal caller
    uses ``eval_from`` as the slice point so the skipped bars never cross the
    FFI. ``dates`` is the full Data1 EL-date column (int sequence).
    """
    accel = _import_accel()
    ast = parse_source(el_source)
    program_ir = serialise_ir(ast)
    try:
        return cast("tuple[int, bool, str]", accel.audit_skip_eval_from(program_ir, dates, inputs or {}))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def _sliced_series_dict(bars: Any, start: int) -> Series:
    """The FFI ``series`` shape over ``bars[start:]`` — slice before box.

    Slices the column views (zero-copy) before materialising Python lists, so
    the ``[0:start]`` prefix (the skip-region bars a proof showed cannot trade)
    is never boxed into Python floats/ints or copied back to Rust.
    """
    cols = ohlcv_columns(bars)
    return {
        "open": list(cols["open"][start:]),
        "high": list(cols["high"][start:]),
        "low": list(cols["low"][start:]),
        "close": list(cols["close"][start:]),
        "volume": list(cols["volume"][start:]),
        "date": [int(d) for d in cols["date"][start:]],
        "time": [int(t) for t in cols["time"][start:]],
    }


def run_native_audit_packet_inputs(
    el_source: str,
    bars: Any,
    config: "InstrumentConfig",
    inputs: dict[str, Any],
    *,
    data2_bars: Any = None,
    eval_from: int = 0,
    source_bar_count: Optional[int] = None,
) -> bytes:
    """One audit-event-v1 packet with explicit ``inputs`` (the solo reference).

    ``bars`` may be the full tape (``eval_from=0``, ``source_bar_count=None``)
    or an already-sliced tape with explicit ``eval_from`` + ``source_bar_count``
    so the packet header stays byte-identical to the full-tape packet.
    """
    accel, program_ir, series, data2 = _serialise_and_marshal(
        el_source, bars, data2_bars
    )
    config_map = _config_dict(config)
    sc = source_bar_count if source_bar_count is not None else len(bars)
    try:
        return cast("bytes", accel.run_program_audit_packet_inputs(
            program_ir, series, data2, config_map, inputs, eval_from, sc
        ))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def run_native_audit_packet_batch(
    el_source: str,
    bars: Any,
    config: "InstrumentConfig",
    variants: list[dict[str, Any]],
    *,
    data2_bars: Any = None,
    eval_from: int = 0,
    source_bar_count: Optional[int] = None,
) -> list[bytes]:
    """K audit-event-v1 packets over ONE tape, compiled + marshalled once.

    ``out[i]`` is byte-identical to ``run_native_audit_packet_inputs`` with
    ``variants[i]``. Pass ``eval_from`` + ``source_bar_count`` (and the sliced
    ``bars``) to run slice-before-marshal; omit them for the unsliced case.
    """
    accel, program_ir, series, data2 = _serialise_and_marshal(
        el_source, bars, data2_bars
    )
    config_map = _config_dict(config)
    sc = source_bar_count if source_bar_count is not None else len(bars)
    try:
        return cast("list[bytes]", accel.run_program_audit_packet_batch(
            program_ir, series, data2, config_map, variants, eval_from, sc
        ))
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc


def run_native_audit_packet_sweep(el_source: str, bars: Any, config: "InstrumentConfig",
                                  variants: list[dict[str, Any]], *, data2_bars: Any = None
                                  ) -> tuple[list[bytes], int, int, str]:
    """Slice-before-marshal sweep: K packets over ONE tape, slice + marshal once.

    Proves the prefix skip from the Data1 dates, slices the columns to
    ``[eval_from:]`` BEFORE boxing them across the FFI, marshals the slice once,
    then emits K packets (compiled once). Returns
    ``(packets, eval_from, source_bar_count, skip_reason)``.
    """
    accel = _import_accel()
    ast = parse_source(el_source)
    program_ir = serialise_ir(ast)
    config_map = _config_dict(config)

    data1_cols = ohlcv_columns(bars)
    n = data1_cols["n"]
    dates = [int(d) for d in data1_cols["date"]]
    try:
        eval_from, proven, reason = accel.audit_skip_eval_from(program_ir, dates, {})
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc

    series = _sliced_series_dict(bars, eval_from)
    data2 = _sliced_series_dict(data2_bars, eval_from) if data2_bars is not None else None

    try:
        packets = accel.run_program_audit_packet_batch(
            program_ir, series, data2, config_map, variants, eval_from, n
        )
    except NotImplementedError as exc:
        _record_fallback()
        raise AccelRefused(str(exc)) from exc

    return packets, eval_from, n, reason
