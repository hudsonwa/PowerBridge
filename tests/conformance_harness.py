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

"""Small-tape differential conformance harness for the optional accel path.

Programs and tapes are deterministic for a given seed.  A mismatch is reduced
over AST statements, block wrappers, and bars before the assertion is rendered,
so CI prints a directly reproducible EL program and JSON tape.
"""

import argparse
import functools
import json
import os
import random
import sys
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import catalog  # noqa: E402
from pl_transpiler.parser import BUILTIN_ARITY  # noqa: E402
from pl_transpiler.reverse import emit_el, parse_el  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    AccelUnavailable,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import run_el  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

DEFAULT_SEED = 20260920
DEFAULT_RANDOM_PROGRAMS = 6
DEFAULT_RANDOM_TAPES = 4
CORPUS_PATH = os.path.join(REPO, "tests", "conformance_corpus.json")
NQ = get_config("NQ")
TRADE_FIELDS = ("type", "signal", "date", "time", "price", "contracts")


@dataclass(frozen=True)
class ConformanceCase:
    case_id: str
    el_source: str
    bars: list
    seed: int
    tags: tuple = ()
    mutation: tuple = ()


class ConformanceMismatch(AssertionError):
    """Exact trade-list mismatch with its minimized reproducer."""

    def __init__(self, message, *, el_source, bars):
        super().__init__(message)
        self.el_source = el_source
        self.bars = bars


def python_trade_runner(el_source, bars):
    """Reference path: emitted Python + run_bar_loop + FillEngine."""
    return run_oracle(el_source, bars, NQ, max_bars_back=0)["records"]


def accel_trade_runner(accel_module):
    """Return the native runner over the stable ``run_program`` contract.

    ``accel_module`` is ``pl_transpiler._accel``, already imported by the caller;
    a successful import is the caller's proof the extension is present.  The
    runner delegates to ``pl_transpiler.runtime.accel.run_native``, which owns
    the single FFI crossing (serialise the IR, then one ``run_program`` call)
    and returns the ordered fills in ``get_mc_trades`` / ``FillEngine.fills``
    shape for the exact trade-list gate.  A construct the kernel cannot execute
    faithfully raises ``AccelRefused``; it never returns a partial or
    approximate trade list.
    """

    def runner(el_source, bars):
        return run_native(el_source, bars, NQ)

    return runner


# The native kernel refuses any program it cannot execute faithfully (a
# ``NotImplementedError`` from the kernel, surfaced by ``run_native`` as
# ``AccelRefused``).  That is a *fallback*, not a mismatch: the kernel is
# honestly declining a construct it does not implement yet (e.g. ``For`` /
# ``While`` loops).  A program the kernel DOES accept must match the Python trade
# list bit-for-bit — a divergence there is a defect and fails the gate.  The
# extension being absent mid-suite is likewise a fallback, so ``run_suite`` never
# reports a false failure for the "extra absent" state.
_NATIVE_FALLBACK_ERRORS = frozenset(
    (AccelRefused.__name__, AccelUnavailable.__name__)
)

# (case_id, reason) for every case the native path declined this process. Kept so
# a run is transparent about how many cases were actually native-parity-checked
# versus how many fell back to "not yet natively implemented".
_fallback_records = []


def fallback_records():
    """List of ``(case_id, reason)`` for native fallbacks recorded so far."""
    return list(_fallback_records)


def _record_fallback(case_id, reason):
    _fallback_records.append((case_id, reason))


def canonical_trade(trade):
    """Tuple used for exact count/order/fill comparison.

    Price uses ``float.hex`` so signed zero and infinities are stable and a
    NaN fill does not compare unequal merely because IEEE NaN is non-reflexive.
    """
    missing = [field for field in TRADE_FIELDS if field not in trade]
    if missing:
        raise ValueError("trade record missing fields: " + ", ".join(missing))

    strings = []
    for field in ("type", "signal"):
        value = trade[field]
        if not isinstance(value, str):
            raise ValueError(
                f"trade record field {field!r} is not a string: {value!r}"
            )
        strings.append(value)

    integers = []
    for field in ("date", "time", "contracts"):
        value = trade[field]
        if isinstance(value, bool):
            raise ValueError(
                f"trade record field {field!r} is not an integer: {value!r}"
            )
        try:
            normalized = int(value)
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError(
                f"trade record field {field!r} is not an integer: {value!r}"
            ) from err
        if value != normalized:
            raise ValueError(
                f"trade record field {field!r} is not an integer: {value!r}"
            )
        integers.append(normalized)

    return (
        strings[0],
        strings[1],
        integers[0],
        integers[1],
        float(trade["price"]).hex(),
        integers[2],
    )


def canonical_trades(trades):
    return [canonical_trade(trade) for trade in trades]


def _canonical_difference(py, native):
    if len(py) != len(native):
        return f"fill count differs: python={len(py)}, accel={len(native)}"
    for index, (want, got) in enumerate(zip(py, native)):
        if want != got:
            return (
                f"fill {index} differs (ordered, zero-based):\n"
                f"  python={want!r}\n"
                f"  accel ={got!r}"
            )
    return None


def trade_difference(python_trades, accel_trades):
    """Return a concise first difference, or None for exact equality."""
    return _canonical_difference(
        canonical_trades(python_trades),
        canonical_trades(accel_trades),
    )


def _bar(date, time, price, rng, *, flat=False):
    if flat:
        open_ = high = low = close = float(price)
    else:
        open_ = float(price)
        close = open_ + rng.choice((-1.0, -0.25, 0.25, 1.0))
        high = max(open_, close) + rng.choice((0.0, 0.25, 0.5))
        low = min(open_, close) - rng.choice((0.0, 0.25, 0.5))
    return {
        "date": int(date),
        "time": int(time),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": float(rng.randint(0, 100000)),
    }


def _edge_close_bar(date, time, close, base, rng):
    """Keep fill-path OHLC bounded while presenting an extreme Close value."""
    bar = _bar(date, time, base, rng, flat=True)
    bar["close"] = float(close)
    bar["high"] = max(bar["open"], bar["close"])
    bar["low"] = min(bar["open"], bar["close"])
    return bar


def generate_tape(seed, random_bars=8):
    """Generate a small tape containing every adversarial tape category.

    Values inside the required skeleton and all additional bars are randomized,
    while timestamps remain sorted.  The skeleton guarantees gaps, a one-bar
    day, a flat bar, an extreme/denormal price, a duplicate timestamp, and both
    sides of common session boundaries.
    """
    rng = random.Random(seed)
    base = rng.uniform(80.0, 180.0)
    day0 = 1240101 + rng.randint(0, 8)
    duplicate_time = rng.choice((900, 930, 1000, 1200))
    bars = [
        _bar(day0, 829, base, rng),
        _bar(day0, 830, base + 1, rng),
        _bar(day0, duplicate_time, base + 2, rng),
        _bar(day0, duplicate_time, base + 3, rng, flat=True),
        _bar(day0, 1559, base - 2, rng),
        _bar(day0, 1600, base - 1, rng),
        _bar(day0 + 1, rng.choice((900, 930, 1200)), base + 4, rng),
        _edge_close_bar(day0 + 2, 829, 1.0e-45, base, rng),
        _edge_close_bar(day0 + 2, 830, 3.4028235e38, base, rng),
        _edge_close_bar(day0 + 2, 1559, 1.0e39, base, rng),
        _bar(day0 + 2, 1600, base, rng),
    ]

    times = (831, 900, 1000, 1200, 1400, 1500)
    additions = []
    for i in range(random_bars):
        date = day0 + 3 + (i // len(times))
        time = rng.choice(times)
        price = base + rng.uniform(-20.0, 20.0)
        additions.append(_bar(date, time, price, rng, flat=rng.random() < 0.15))
    additions.sort(key=lambda b: (b["date"], b["time"]))
    return bars + additions


def tape_features(bars):
    """Return the adversarial categories observed in a tape."""
    features = set()
    stamps = [(int(b["date"]), int(b["time"])) for b in bars]
    if len(stamps) != len(set(stamps)):
        features.add("duplicate_timestamp")
    counts = {}
    for date, _time in stamps:
        counts[date] = counts.get(date, 0) + 1
    if any(count == 1 for count in counts.values()):
        features.add("single_bar_day")
    if any(
        float(b["open"]) == float(b["high"]) == float(b["low"]) == float(b["close"])
        for b in bars
    ):
        features.add("flat_bar")
    values = [
        abs(float(b[field]))
        for b in bars
        for field in ("open", "high", "low", "close")
    ]
    if any(value >= 1.0e38 or 0.0 < value <= 1.0e-38 for value in values):
        features.add("extreme_tick")
    times_by_date = {}
    for date, time in stamps:
        times_by_date.setdefault(date, []).append(time)
    if any(
        later - earlier > 1
        for times in times_by_date.values()
        for earlier, later in zip(times, times[1:])
    ):
        features.add("gap")
    all_times = {time for _date, time in stamps}
    if {829, 830, 1559, 1600}.issubset(all_times):
        features.add("session_boundary")
    return features


def _argument_patterns(arity):
    """Generic expressions used to probe catalogue records at their parser arity."""
    patterns = {
        0: ("",),
        1: ("2",),
        2: ("2, 2",),
        3: ("2, 2, 1",),
    }
    return patterns.get(arity, ())


@functools.lru_cache(maxsize=1)
def catalogue_call_cases():
    """Executable numeric calls and results derived from the keyword catalogue.

    No builtin name is maintained here.  Candidates come from
    ``catalog.search('')``, their return type and arity come from ``catalog``
    and ``parser.BUILTIN_ARITY``, and a Python-path trace probe removes calls
    whose generic arguments are not executable or whose result cannot make a
    useful exact trade gate.
    """
    probe_bars = generate_tape(DEFAULT_SEED, random_bars=0)
    cases = []
    for name in catalog.search(""):
        record = catalog.get(name)
        low_name = name.lower()
        if record is None or str(record["returns"]).lower() != "numeric":
            continue
        if low_name not in BUILTIN_ARITY:
            continue
        selected = None
        for arity in sorted(BUILTIN_ARITY[low_name]):
            for args in _argument_patterns(arity):
                call = f"{record['keyword']}({args})"
                probe = f"Vars: CatalogueProbe(0);\nCatalogueProbe = {call};\n"
                try:
                    output = run_el(
                        probe,
                        probe_bars,
                        NQ,
                        trace_columns=["catalogueprobe"],
                    )
                    values = {float(row[0]) for row in output["rows"]}
                except Exception:
                    continue
                if len(values) != 1:
                    continue
                value = values.pop()
                if value == 0.0 or value != value or abs(value) == float("inf"):
                    continue
                selected = (call, repr(value))
                break
            if selected is not None:
                break
        if selected is not None:
            cases.append(selected)
    if not cases:
        raise AssertionError("catalogue yielded no executable numeric builtins")
    return tuple(cases)


def catalogue_call_pool():
    return tuple(call for call, _expected in catalogue_call_cases())


def generate_el(seed):
    """Generate one deterministic strategy from catalogue-backed constructs."""
    rng = random.Random(seed)
    builtin_call, builtin_expected = rng.choice(catalogue_call_cases())
    entry_every = rng.randint(2, 5)
    exit_every = rng.randint(2, 5)
    loop_limit = rng.randint(1, 4)
    loop_value = loop_limit * (3 * loop_limit - 1) / 4
    compare = rng.choice((">=", "<=", "<>"))
    offset = rng.choice(("0.25", "0.5", "1.0", "2.0"))
    label = f"{seed & 0xffff:04X}"
    return (
        f"Inputs: EntryEvery({entry_every}), ExitEvery({exit_every}), "
        f"LoopLimit({loop_limit}), Offset({offset});\n"
        "Vars: BuiltinValue(0), PriceSeries(0), PriorValue(0), PerBar(0), "
        "OnceValue(0), LoopValue(0), I(0), J(0), Gate(False);\n"
        f"BuiltinValue = {builtin_call};\n"
        "PriceSeries = Close;\n"
        "PriorValue = PriceSeries[1];\n"
        "PerBar = PerBar + 1;\n"
        "Once Begin OnceValue = Close; End;\n"
        "LoopValue = 0;\n"
        "For I = 1 To LoopLimit Begin LoopValue = LoopValue + I / 2; End;\n"
        "J = 0;\n"
        "While J < LoopLimit Begin LoopValue = LoopValue + J; J = J + 1; End;\n"
        f"If Close {compare} Open Then Begin\n"
        "    If BarNumber > 1 and PriorValue <> 0 Then Gate = True\n"
        "    Else Gate = False;\n"
        "End Else Gate = False;\n"
        f"If MarketPosition = 0 and Gate and "
        f"BuiltinValue = {builtin_expected} and "
        f"OnceValue <> Close and LoopValue = {loop_value:g} and "
        f"BarNumber mod EntryEvery = 0 "
        f'Then Buy ("LE_{label}") 1 contract next bar at market;\n'
        f'If MarketPosition = 1 and BarNumber mod ExitEvery = 0 '
        f'Then Sell ("LX_{label}") next bar at market;\n'
    )


def generated_cases(seed=DEFAULT_SEED,
                    program_count=DEFAULT_RANDOM_PROGRAMS,
                    tape_count=DEFAULT_RANDOM_TAPES):
    master = random.Random(seed)
    program_seeds = [
        master.randrange(0, 2 ** 31) for _ in range(program_count)
    ]
    tape_seeds = [
        master.randrange(0, 2 ** 31) for _ in range(tape_count)
    ]
    cases = []
    for program_index, program_seed in enumerate(program_seeds):
        for tape_index, tape_seed in enumerate(tape_seeds):
            cases.append(
                ConformanceCase(
                    case_id=(
                        f"random-p{program_index:03d}-{program_seed}"
                        f"-t{tape_index:03d}-{tape_seed}"
                    ),
                    el_source=generate_el(program_seed),
                    bars=generate_tape(tape_seed),
                    seed=program_seed,
                    tags=("random", "catalogue", f"tape-seed-{tape_seed}"),
                )
            )
    return cases


def load_corpus(path=CORPUS_PATH):
    with open(path, encoding="utf-8") as stream:
        document = json.load(stream)
    tapes = document["tapes"]
    cases = []
    for record in document["cases"]:
        seed = int(record["seed"])
        bars = record.get("bars")
        if bars is None:
            bars = tapes[record["tape"]]
        cases.append(
            ConformanceCase(
                case_id=record["id"],
                el_source=record["el"],
                bars=bars,
                seed=seed,
                tags=tuple(record.get("tags", ())),
                mutation=tuple(record.get("mutation", ())),
            )
        )
    return cases


def _capture_outcome(runner, el_source, bars):
    try:
        return ("trades", canonical_trades(runner(el_source, bars)))
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc))


def _mismatch_signature(python_outcome, accel_outcome):
    """Stable mismatch class so reduction cannot switch to another bug."""
    if python_outcome[0] != "trades":
        return None
    if accel_outcome[0] == "error":
        return ("accel-error", accel_outcome[1], accel_outcome[2])
    python_trades = python_outcome[1]
    accel_trades = accel_outcome[1]
    if len(python_trades) != len(accel_trades):
        direction = -1 if len(python_trades) < len(accel_trades) else 1
        return ("fill-count", direction)
    if python_trades != accel_trades:
        return ("fill-content-or-order",)
    return None


def _is_mismatch(python_runner, accel_runner, el_source, bars,
                 expected_signature=None):
    python_outcome = _capture_outcome(python_runner, el_source, bars)
    accel_outcome = _capture_outcome(accel_runner, el_source, bars)
    signature = _mismatch_signature(python_outcome, accel_outcome)
    if expected_signature is None:
        return signature is not None
    return signature == expected_signature


def _ddmin(items, interesting):
    """Classic deterministic delta debugging over a sequence."""
    current = list(items)
    if current and interesting([]):
        return []
    granularity = 2
    while len(current) >= 2:
        chunk_size = (len(current) + granularity - 1) // granularity
        reduced = False
        for start in range(0, len(current), chunk_size):
            candidate = current[:start] + current[start + chunk_size:]
            if interesting(candidate):
                current = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if reduced:
            continue
        if granularity >= len(current):
            break
        granularity = min(len(current), granularity * 2)
    return current


def _minimize_source(python_runner, accel_runner, el_source, bars,
                     mismatch_signature):
    try:
        ast = parse_el(el_source)
        body = list(ast.get("body", ()))
    except Exception:
        return el_source
    if not body:
        return el_source

    def render(nodes):
        program = {"type": "program", "body": nodes}
        return emit_el(program)

    def interesting(nodes):
        return _is_mismatch(
            python_runner, accel_runner, render(nodes), bars,
            mismatch_signature,
        )

    # Removing only top-level statements leaves avoidable wrappers such as
    # ``If ... Then Buy``.  Try splicing each block body (or else body) into its
    # parent before and after ddmin.  Invalid candidates are rejected because
    # the Python reference must still execute successfully.
    def unwrap(nodes):
        current = list(nodes)
        while True:
            changed = False
            for index, node in enumerate(current):
                if not isinstance(node, dict):
                    continue
                for key in ("body", "else_body"):
                    branch = node.get(key)
                    if not isinstance(branch, list):
                        continue
                    candidate = current[:index] + branch + current[index + 1:]
                    if interesting(candidate):
                        current = candidate
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                return current

    body = unwrap(body)
    reduced = _ddmin(
        body,
        interesting,
    )
    return render(unwrap(reduced))


def minimize_counterexample(python_runner, accel_runner, el_source, bars,
                            mismatch_signature=None):
    """Reduce a mismatch over top-level statements and tape rows."""
    if mismatch_signature is None:
        mismatch_signature = _mismatch_signature(
            _capture_outcome(python_runner, el_source, bars),
            _capture_outcome(accel_runner, el_source, bars),
        )
    source = el_source
    minimal_bars = list(bars)
    while True:
        before = (source, len(minimal_bars))
        source = _minimize_source(
            python_runner, accel_runner, source, minimal_bars,
            mismatch_signature,
        )
        minimal_bars = _ddmin(
            minimal_bars,
            lambda candidate, source=source: _is_mismatch(
                python_runner, accel_runner, source, candidate,
                mismatch_signature,
            ),
        )
        source = _minimize_source(
            python_runner, accel_runner, source, minimal_bars,
            mismatch_signature,
        )
        if before == (source, len(minimal_bars)):
            return source, minimal_bars


def _format_outcome(outcome):
    if outcome[0] == "trades":
        return repr(outcome[1])
    return f"{outcome[1]}: {outcome[2]}"


def assert_case(case, accel_runner, python_runner=python_trade_runner):
    """Assert one case and raise with a minimized EL+tape reproducer."""
    python_outcome = _capture_outcome(
        python_runner, case.el_source, case.bars
    )
    if python_outcome[0] != "trades":
        raise AssertionError(
            f"{case.case_id}: generated/corpus program failed on Python reference: "
            f"{_format_outcome(python_outcome)}"
        )
    accel_outcome = _capture_outcome(
        accel_runner, case.el_source, case.bars
    )
    if accel_outcome[0] == "error" and accel_outcome[1] in _NATIVE_FALLBACK_ERRORS:
        _record_fallback(case.case_id, accel_outcome[2])
        return
    mismatch_signature = _mismatch_signature(
        python_outcome, accel_outcome
    )
    if mismatch_signature is None:
        return

    minimal_el, minimal_bars = minimize_counterexample(
        python_runner, accel_runner, case.el_source, case.bars,
        mismatch_signature,
    )
    minimal_python = _capture_outcome(
        python_runner, minimal_el, minimal_bars
    )
    minimal_accel = _capture_outcome(
        accel_runner, minimal_el, minimal_bars
    )
    if minimal_python[0] == minimal_accel[0] == "trades":
        detail = _canonical_difference(minimal_python[1], minimal_accel[1])
    else:
        detail = (
            "backend outcome differs:\n"
            f"  python={_format_outcome(minimal_python)}\n"
            f"  accel ={_format_outcome(minimal_accel)}"
        )
    tape_json = json.dumps(
        minimal_bars, indent=2, sort_keys=True, allow_nan=True
    )
    message = (
        f"{case.case_id}: Python/accel trade-list mismatch (seed={case.seed})\n"
        f"{detail}\n"
        "--- minimal failing EL ---\n"
        f"{minimal_el.rstrip()}\n"
        "--- minimal failing tape (JSON) ---\n"
        f"{tape_json}"
    )
    raise ConformanceMismatch(
        message, el_source=minimal_el, bars=minimal_bars
    )


def run_suite(accel_runner, *, seed=DEFAULT_SEED,
              program_count=DEFAULT_RANDOM_PROGRAMS,
              tape_count=DEFAULT_RANDOM_TAPES):
    cases = load_corpus() + generated_cases(
        seed, program_count, tape_count
    )
    for case in cases:
        assert_case(case, accel_runner)
    return cases


def prove_deliberate_mismatch():
    """Run one intentionally bad backend and return its rendered failure."""
    case = load_corpus()[0]

    def broken_runner(el_source, bars):
        trades = python_trade_runner(el_source, bars)
        return trades[:-1] if trades else trades

    try:
        assert_case(case, broken_runner)
    except ConformanceMismatch as exc:
        return str(exc)
    raise AssertionError("deliberately broken backend was not detected")


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Python-vs-accel exact trade-list conformance harness"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--random-programs", type=int, default=DEFAULT_RANDOM_PROGRAMS
    )
    parser.add_argument(
        "--random-tapes", type=int, default=DEFAULT_RANDOM_TAPES
    )
    parser.add_argument(
        "--prove-mismatch",
        action="store_true",
        help="run an intentionally broken backend and print the minimized failure",
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.prove_mismatch:
        print(prove_deliberate_mismatch())
        return 0

    try:
        import pl_transpiler._accel as accel
    except ImportError:
        print("SKIP: " + ACCEL_SKIP)
        return 0

    cases = run_suite(
        accel_trade_runner(accel),
        seed=args.seed,
        program_count=args.random_programs,
        tape_count=args.random_tapes,
    )
    random_cases = args.random_programs * args.random_tapes
    fallbacks = fallback_records()
    print(
        f"PASS: {len(cases)} conformance cases "
        f"({len(load_corpus())} corpus, {random_cases} randomized: "
        f"{args.random_programs} programs x {args.random_tapes} tapes), "
        f"{len(fallbacks)} native fallback"
        f"{'' if len(fallbacks) == 1 else 's'}, "
        f"seed={args.seed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
