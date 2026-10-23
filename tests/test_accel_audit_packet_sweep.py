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

"""Slice-before-marshal audit-packet sweep.

Proves, on the fix-mid fixture and the shipped NQ EL, that the sliced batch is
byte-identical to the full-tape packet (``sliced == full``), that the audit
config's packet reproduces the shipped trades.csv digest, and that every config
in a batch is bit-identical to that config run solo. Skips cleanly when the
extension or the fixture is absent.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from pl_transpiler.runtime import accel  # noqa: E402
from pl_transpiler.runtime.audit_packet import decode, write_trades_csv  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from pl_transpiler.ir import serialise_ir  # noqa: E402
from pl_transpiler.lexer import tokenise  # noqa: E402
from pl_transpiler.parser import parse  # noqa: E402

# Private fixture (not shipped): point these at a local copy to run the sweep.
FIXTURE_DIR = os.environ.get("PB_AUDIT_FIXTURE_DIR", "")
EL_PATH = os.environ.get("PB_AUDIT_EL", "")
GOLDEN_TRADES_SHA = "3f52271ba2c8ed2ede83413a466046a9da9c46a072ec38a96c28425a4de808f8"

NQ = get_config("NQ")


def _require():
    if not accel.accel_available():
        pytest.skip(ACCEL_SKIP)
    for path in (FIXTURE_DIR, EL_PATH):
        if not path or not os.path.exists(path):
            pytest.skip("private fixture absent — set PB_AUDIT_FIXTURE_DIR / PB_AUDIT_EL")


def _program_ir(el_source):
    tokens, positions = tokenise(el_source)
    ast = parse(tokens, positions=positions, source=el_source)
    return serialise_ir(ast)


def _trades_sha(packet, tmp_path):
    path = tmp_path / "trades.csv"
    write_trades_csv(path, decode(packet)["trades"])
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup():
    el = open(EL_PATH, encoding="utf-8").read()
    bars = load_bars_csv(os.path.join(FIXTURE_DIR, "bars.csv"))
    data2 = load_bars_csv(os.path.join(FIXTURE_DIR, "bars_data2.csv"))
    return el, bars, data2


@pytest.mark.needs_native
@pytest.mark.needs_captures
def test_sliced_batch_reproduces_full_packet_and_golden(tmp_path):
    _require()
    el, bars, data2 = _setup()
    n = len(bars)

    full = accel.run_native_audit_packet(el, bars, NQ, data2_bars=data2)

    dates = [int(b["date"]) for b in bars]
    eval_from, proven, _ = accel.audit_skip_eval_from(el, dates, {})
    assert proven, "the NQ EL must prove a prefix skip"
    assert eval_from > 0

    # slice-then-marshal; the batch is compiled+extracted once over the slice.
    packets, ef, src, _ = accel.run_native_audit_packet_sweep(
        el, bars, NQ, [{}], data2_bars=data2
    )
    assert (ef, src) == (eval_from, n)
    assert packets[0] == full, "sliced packet must be byte-identical to the full-tape packet"

    assert _trades_sha(packets[0], tmp_path) == GOLDEN_TRADES_SHA
    hdr = decode(packets[0])["header"]
    assert hdr["fill_count"] == 7364
    assert hdr["round_turn_count"] == 3682


@pytest.mark.needs_native
@pytest.mark.needs_captures
def test_every_config_in_batch_is_byte_identical_to_solo(tmp_path):
    _require()
    el, bars, data2 = _setup()

    variants = [{}, {"C2Fraction": 0.002}, {"EntryMultiplier": 0.6}, {"LadderLength": 20.0}]

    packets, eval_from, src, _ = accel.run_native_audit_packet_sweep(
        el, bars, NQ, variants, data2_bars=data2
    )

    ir = _program_ir(el)
    config_map = accel._config_dict(NQ)
    series = accel._sliced_series_dict(bars, eval_from)
    data2_series = accel._sliced_series_dict(data2, eval_from)
    ffi = accel._import_accel()
    for i, variant in enumerate(variants):
        solo = ffi.run_program_audit_packet_inputs(
            ir, series, data2_series, config_map, variant, eval_from, src
        )
        assert solo == packets[i], f"variant {i} batch diverges from solo"
