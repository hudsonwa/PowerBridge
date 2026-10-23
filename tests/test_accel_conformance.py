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

"""Portable checks and optional Python-vs-accel differential gate."""

import math
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import catalog  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import run_el  # noqa: E402
from tests.conformance_harness import (  # noqa: E402
    DEFAULT_RANDOM_PROGRAMS,
    DEFAULT_RANDOM_TAPES,
    DEFAULT_SEED,
    ConformanceMismatch,
    accel_trade_runner,
    assert_case,
    catalogue_call_cases,
    catalogue_call_pool,
    generate_el,
    generate_tape,
    generated_cases,
    load_corpus,
    python_trade_runner,
    run_suite,
    tape_features,
    trade_difference,
)
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")


def test_catalogue_drives_random_builtin_pool():
    calls = catalogue_call_pool()
    assert len(calls) >= 5
    for call in calls:
        assert catalog.get(call.split("(", 1)[0]) is not None
    assert all(float(expected) != 0.0 for _call, expected in catalogue_call_cases())


def test_seed_reproduces_programs_and_tapes():
    assert generate_el(DEFAULT_SEED) == generate_el(DEFAULT_SEED)
    assert generate_tape(DEFAULT_SEED) == generate_tape(DEFAULT_SEED)
    first = generated_cases(DEFAULT_SEED, 4, 3)
    second = generated_cases(DEFAULT_SEED, 4, 3)
    assert first == second
    assert len({case.el_source for case in first}) == 4
    assert len({str(case.bars) for case in first}) == 3


def test_random_tape_contains_required_adversarial_shapes():
    expected = {
        "gap",
        "single_bar_day",
        "flat_bar",
        "extreme_tick",
        "duplicate_timestamp",
        "session_boundary",
    }
    for seed in (DEFAULT_SEED, 1, 0x5A17):
        bars = generate_tape(seed)
        assert tape_features(bars) == expected
        for bar in bars:
            assert bar["low"] <= min(bar["open"], bar["close"])
            assert bar["high"] >= max(bar["open"], bar["close"])


def test_committed_corpus_covers_tricky_constructs_and_trades():
    cases = load_corpus()
    tags = {tag for case in cases for tag in case.tags}
    assert {
        "nested-if",
        "once",
        "once-per-bar",
        "while",
        "for",
        "integer-division",
        "float-division",
        "f32",
        "series-assignment",
        "inputs",
        "extreme",
        "nan",
        "denormal",
    }.issubset(tags)
    assert len(cases) == 10
    for case in cases:
        trades = python_trade_runner(case.el_source, case.bars)
        assert trades, case.case_id
        assert len(case.mutation) == 2
        mutated = case.el_source.replace(*case.mutation)
        assert mutated != case.el_source
        assert python_trade_runner(mutated, case.bars) != trades, case.case_id


def test_corpus_reaches_float_edge_values():
    cases = {case.case_id: case for case in load_corpus()}

    division = cases["integer-float-division"]
    rows = run_el(
        division.el_source,
        division.bars,
        NQ,
        trace_columns=["intdiv", "floatdiv"],
    )["rows"]
    assert all(tuple(map(float, row)) == (2.5, 2.5) for row in rows)

    boundary = cases["f32-rounding-boundary"]
    rows = run_el(
        boundary.el_source,
        boundary.bars,
        NQ,
        trace_columns=["rounded", "boundary"],
    )["rows"]
    assert all(
        tuple(map(float, row)) == (16777216.0, 16777216.0)
        for row in rows
    )

    overflow = cases["extreme-overflow"]
    values = run_el(
        overflow.el_source,
        overflow.bars,
        NQ,
        trace_columns=["extreme"],
    )["rows"]
    assert any(math.isinf(float(row[0])) for row in values)

    nan_case = cases["nan-arithmetic"]
    values = run_el(
        nan_case.el_source,
        nan_case.bars,
        NQ,
        trace_columns=["notanumber"],
    )["rows"]
    assert any(math.isnan(float(row[0])) for row in values)

    denormal = cases["denormal-underflow"]
    values = run_el(
        denormal.el_source,
        denormal.bars,
        NQ,
        trace_columns=["denormal"],
    )["rows"]
    tiny_index = next(
        index
        for index, bar in enumerate(denormal.bars)
        if bar["close"] == 1e-45
    )
    assert float(values[tiny_index][0]) == 0.0


def test_randomized_program_tape_cross_product_runs_reference():
    cases = generated_cases(
        DEFAULT_SEED, DEFAULT_RANDOM_PROGRAMS, DEFAULT_RANDOM_TAPES
    )
    assert len(cases) == DEFAULT_RANDOM_PROGRAMS * DEFAULT_RANDOM_TAPES
    assert len({case.el_source for case in cases}) == DEFAULT_RANDOM_PROGRAMS
    assert len({str(case.bars) for case in cases}) == DEFAULT_RANDOM_TAPES
    for case in cases:
        assert python_trade_runner(case.el_source, case.bars), case.case_id


def test_generated_constructs_change_the_trade_oracle():
    cases = generated_cases(DEFAULT_SEED, DEFAULT_RANDOM_PROGRAMS, 1)
    for case in cases:
        reference = python_trade_runner(case.el_source, case.bars)
        builtin_line = next(
            line for line in case.el_source.splitlines()
            if line.startswith("BuiltinValue = ")
        )
        mutations = (
            (builtin_line, "BuiltinValue = -1;"),
            ("Once Begin OnceValue = Close; End;", "OnceValue = Close;"),
            (
                "For I = 1 To LoopLimit Begin "
                "LoopValue = LoopValue + I / 2; End;",
                "For I = 1 To LoopLimit Begin "
                "LoopValue = LoopValue + 0; End;",
            ),
            (
                "While J < LoopLimit Begin "
                "LoopValue = LoopValue + J; J = J + 1; End;",
                "While J < LoopLimit Begin "
                "LoopValue = LoopValue + 0; J = J + 1; End;",
            ),
            ("PriorValue = PriceSeries[1];", "PriorValue = 0;"),
        )
        for old, new in mutations:
            mutated = case.el_source.replace(old, new)
            assert mutated != case.el_source
            assert python_trade_runner(mutated, case.bars) != reference, (
                case.case_id, old
            )


def test_comparator_checks_order_and_every_fill_field():
    first = {
        "type": "EntryLong",
        "signal": "LE",
        "date": 1240102,
        "time": 930,
        "price": 100.0,
        "contracts": 1,
    }
    second = {
        "type": "ExitLong",
        "signal": "LX",
        "date": 1240102,
        "time": 931,
        "price": 101.0,
        "contracts": 1,
    }
    assert trade_difference([first, second], [first, second]) is None
    assert "fill 0 differs" in trade_difference(
        [first, second], [second, first]
    )
    changed = dict(second, price=101.25)
    assert "fill 1 differs" in trade_difference(
        [first, second], [first, changed]
    )
    with pytest.raises(ValueError, match="'time' is not an integer"):
        trade_difference([first], [dict(first, time=930.5)])


def test_deliberately_broken_backend_reports_minimal_el_and_tape():
    case = load_corpus()[0]

    def drop_last_fill(el_source, bars):
        trades = python_trade_runner(el_source, bars)
        return trades[:-1] if trades else trades

    with pytest.raises(ConformanceMismatch) as caught:
        assert_case(case, drop_last_fill)
    report = str(caught.value)
    assert "fill count differs" in report
    assert "--- minimal failing EL ---" in report
    assert "--- minimal failing tape (JSON) ---" in report
    assert len(caught.value.bars) == 2
    assert caught.value.el_source.strip().lower().startswith("buy")


def test_full_suite_accepts_identical_trade_lists():
    cases = run_suite(
        python_trade_runner,
        seed=DEFAULT_SEED,
        program_count=DEFAULT_RANDOM_PROGRAMS,
        tape_count=DEFAULT_RANDOM_TAPES,
    )
    assert len(cases) == (
        len(load_corpus()) + DEFAULT_RANDOM_PROGRAMS * DEFAULT_RANDOM_TAPES
    )


@pytest.mark.needs_native
def test_accel_runner_delegates_to_frozen_run_program():
    try:
        import pl_transpiler._accel as accel
    except ImportError:
        pytest.skip(ACCEL_SKIP)

    runner = accel_trade_runner(accel)
    assert callable(runner)
    assert callable(getattr(accel, "run_program", None))


@pytest.mark.needs_native
def test_python_vs_accel_identical_trade_lists():
    try:
        import pl_transpiler._accel as accel
    except ImportError:
        pytest.skip(ACCEL_SKIP)

    cases = run_suite(
        accel_trade_runner(accel),
        seed=DEFAULT_SEED,
        program_count=DEFAULT_RANDOM_PROGRAMS,
        tape_count=DEFAULT_RANDOM_TAPES,
    )
    assert len(cases) == (
        len(load_corpus()) + DEFAULT_RANDOM_PROGRAMS * DEFAULT_RANDOM_TAPES
    )
