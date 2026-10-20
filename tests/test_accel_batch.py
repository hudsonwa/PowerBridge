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

"""The native sweep/batch lane (issue #24): ``run_program_batch`` /
``run_program_batch_parallel`` / ``run_program_inputs``.

Proves, when ``pl_transpiler._accel`` is built:

  * a batch of input variants over ONE already-marshalled tape is per-variant
    bit-identical to a solo ``run_program_inputs`` run (solo_digest ==
    batch_digest);
  * the parallel batch is bit-identical to the sequential batch — parallelism
    never changes an answer;
  * the default (``{}``) variant equals the untouched ``run_program`` path.

Every native-path test skips cleanly (``pytest.skip``) when the extension is not
built.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from pl_transpiler.ir import serialise_ir  # noqa: E402
from pl_transpiler.lexer import tokenise  # noqa: E402
from pl_transpiler.parser import parse  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    _config_dict,
    accel_available,
    marshalled_series,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

NQ = get_config("NQ")
SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")


def _require_accel():
    if not accel_available():
        pytest.skip(ACCEL_SKIP)


def _program_ir(el_source):
    tokens, positions = tokenise(el_source)
    ast = parse(tokens, positions=positions, source=el_source)
    return serialise_ir(ast)


def _ffi():
    import pl_transpiler._accel as accel
    return accel


def _setup():
    el = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLES)
    series, _ = marshalled_series(bars)
    cmap = _config_dict(NQ)
    return el, bars, series, cmap


def test_batch_default_variant_matches_run_program():
    _require_accel()
    accel = _ffi()
    el, _, series, cmap = _setup()
    pir = _program_ir(el)

    default = accel.run_program(pir, series, None, cmap)
    batch = accel.run_program_batch(pir, series, None, cmap, [{}])

    assert len(batch) == 1
    assert fills_sha256(batch[0]) == fills_sha256(default)


def test_batch_variants_match_solo_inputs():
    _require_accel()
    accel = _ffi()
    el, _, series, cmap = _setup()
    pir = _program_ir(el)

    variants = [{}, {"FastLen": 20.0}, {"slowlen": 60.0}, {"fastlen": 5.0, "slowlen": 15.0}]
    batch = accel.run_program_batch(pir, series, None, cmap, variants)

    assert len(batch) == len(variants)
    for i, variant in enumerate(variants):
        solo = accel.run_program_inputs(pir, series, None, cmap, variant)
        assert fills_sha256(batch[i]) == fills_sha256(solo), f"variant {i}"


def test_batch_parallel_is_bit_identical_to_sequential():
    _require_accel()
    accel = _ffi()
    el, _, series, cmap = _setup()
    pir = _program_ir(el)

    variants = [{}, {"fastlen": 20.0}, {"slowlen": 60.0}, {"fastlen": 5.0}]
    seq = accel.run_program_batch(pir, series, None, cmap, variants)
    par = accel.run_program_batch_parallel(pir, series, None, cmap, variants, threads=3)

    assert len(seq) == len(par) == len(variants)
    for i in range(len(variants)):
        assert fills_sha256(seq[i]) == fills_sha256(par[i]), f"variant {i}"


def test_run_program_inputs_empty_equals_run_program():
    _require_accel()
    accel = _ffi()
    el, _, series, cmap = _setup()
    pir = _program_ir(el)

    a = accel.run_program(pir, series, None, cmap)
    b = accel.run_program_inputs(pir, series, None, cmap, {})
    assert fills_sha256(a) == fills_sha256(b)


def test_batch_requires_variants_list():
    _require_accel()
    accel = _ffi()
    el, _, series, cmap = _setup()
    pir = _program_ir(el)

    with pytest.raises(ValueError):
        accel.run_program_batch(pir, series, None, cmap)


def test_run_native_sweep_public_api_matches_solo():
    _require_accel()
    from pl_transpiler.runtime.accel import run_native_inputs, run_native_sweep

    el = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLES)
    variants = [{}, {"fastlen": 20.0}, {"slowlen": 60.0}]

    seq = run_native_sweep(el, bars, NQ, variants)
    par = run_native_sweep(el, bars, NQ, variants, threads=3)

    for i, variant in enumerate(variants):
        solo = run_native_inputs(el, bars, NQ, variant)
        assert fills_sha256(seq[i]) == fills_sha256(solo), f"seq variant {i}"
        assert fills_sha256(par[i]) == fills_sha256(solo), f"par variant {i}"


# Mixed-case Input override fixture: numeric default keeps the orders live;
# a slightly larger override must produce the empty trade list.
_MYPARAM_EL = """
Inputs: MyParam( 0.10 ) ;

If MyParam <= 0.10 Then
	Buy ( "LE" ) 1 contract next bar at market ;
If MyParam <= 0.10 Then
	Sell ( "SE" ) 1 contract next bar at market ;
"""


def test_source_spelling_override_changes_trade_list():
    """``{"MyParam": 0.101}`` must apply against IR name ``myparam``.

    The lexer folds identifiers; callers pass the source spelling.
    Before the fix every variant returned the default digest.
    """
    _require_accel()
    from pl_transpiler.runtime.accel import run_native, run_native_inputs, run_native_sweep

    bars = load_bars_csv(SAMPLES)
    default = run_native(_MYPARAM_EL, bars, NQ)
    empty = run_native_inputs(_MYPARAM_EL, bars, NQ, {})
    folded = run_native_inputs(_MYPARAM_EL, bars, NQ, {"myparam": 0.101})
    source = run_native_inputs(_MYPARAM_EL, bars, NQ, {"MyParam": 0.101})

    assert fills_sha256(empty) == fills_sha256(default), "{} must equal run_native defaults"
    assert default, "default MyParam=0.10 must produce fills (non-vacuous)"
    assert fills_sha256(source) != fills_sha256(default), "MyParam=0.101 must change the trade list"
    assert fills_sha256(source) == fills_sha256(folded), "MyParam and myparam must resolve identically"
    assert source == [], "MyParam=0.101 must suppress the gated orders"

    variants = [{}, {"MyParam": 0.101}, {"myparam": 0.101}]
    batch = run_native_sweep(_MYPARAM_EL, bars, NQ, variants)
    par = run_native_sweep(_MYPARAM_EL, bars, NQ, variants, threads=2)
    assert fills_sha256(batch[0]) == fills_sha256(default)
    assert fills_sha256(batch[1]) == fills_sha256(source)
    assert fills_sha256(batch[2]) == fills_sha256(source)
    assert fills_sha256(par[1]) == fills_sha256(source)


def test_gt2_fastlen_source_spelling_matches_folded_and_batch():
    """GT2 writes ``FastLen``; override maps using that spelling must apply."""
    _require_accel()
    from pl_transpiler.runtime.accel import run_native, run_native_inputs, run_native_sweep

    el = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLES)
    default = run_native(el, bars, NQ)
    source = run_native_inputs(el, bars, NQ, {"FastLen": 20.0})
    folded = run_native_inputs(el, bars, NQ, {"fastlen": 20.0})

    assert fills_sha256(source) != fills_sha256(default), "FastLen=20 must change GT2 fills"
    assert fills_sha256(source) == fills_sha256(folded)

    variants = [{}, {"FastLen": 20.0}]
    batch = run_native_sweep(el, bars, NQ, variants)
    assert fills_sha256(batch[0]) == fills_sha256(default)
    assert fills_sha256(batch[1]) == fills_sha256(source)
