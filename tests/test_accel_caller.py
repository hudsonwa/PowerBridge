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

"""The Python-side native caller (issue #4): ``pl_transpiler.runtime.accel``.

Proves, when ``pl_transpiler._accel`` is built:

  * the native path is taken when the program is supported — exactly one FFI
    ``run_program`` crossing per run, and its fills are bit-identical to the
    Python ``run_oracle`` trade list (so the digest is identical);
  * a supported Data2 program runs natively and matches Python bit-for-bit;
  * on refusal the caller raises loudly (``AccelRefused``) and ``run_oracle``
    falls back to Python with a stderr note — never a partial trade list;
  * ``run_oracle(use_native=True)`` and the pure-Python path produce the same
    digest text.

Every native-path test skips cleanly (``pytest.skip``) when the extension is
not built — a captures-less / no-Rust clone must stay green, and the skip must
never masquerade as a pass.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.skip_messages import ACCEL_SKIP  # noqa: E402
from pl_transpiler.runtime.accel import (  # noqa: E402
    AccelRefused,
    accel_available,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256, run_oracle  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.conformance_harness import trade_difference  # noqa: E402

NQ = get_config("NQ")
SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")

DATA2_SOURCE = (
    "Variables: D2C(0);\n"
    "D2C = Close of Data2;\n"
    "If D2C > Close Then Buy (\"D2B\") 1 Contracts Next Bar At D2C Limit;\n"
)


def _require_accel():
    if not accel_available():
        pytest.skip(ACCEL_SKIP)


def _sample_bars():
    return load_bars_csv(SAMPLES)


def test_native_path_one_ffi_crossing_and_bit_identical():
    _require_accel()
    import pl_transpiler._accel as accel

    gt2 = open(GT2, encoding="utf-8").read()
    bars = _sample_bars()
    real_run_program = accel.run_program
    calls = []

    def spy(program_ir, series, data2, config):
        calls.append((program_ir, series, data2, config))
        return real_run_program(program_ir, series, data2, config)

    accel.run_program = spy
    try:
        native_fills = run_native(gt2, bars, NQ)
    finally:
        accel.run_program = real_run_program

    assert len(calls) == 1, "run_native must cross the FFI exactly once per run"
    program_ir, series, data2, config = calls[0]
    payload = json.loads(program_ir)
    assert payload["kind"] == "pl_transpiler.ast"
    assert payload["ir_version"] == 1
    assert payload["ast"]["type"] == "program"
    assert list(series) == ["open", "high", "low", "close", "volume", "date", "time"]
    assert data2 is None
    assert config["name"] == "NQ"

    python_records = run_oracle(gt2, bars, NQ)["records"]
    assert trade_difference(python_records, native_fills) is None
    assert fills_sha256(native_fills) == fills_sha256(python_records)


def test_native_data2_matches_python_bit_exactly():
    _require_accel()
    bars = _sample_bars()
    data2_tape = [dict(b, close=b["close"] + 3.0) for b in bars]
    data2_closes = [b["close"] for b in data2_tape]

    python_records = run_oracle(
        DATA2_SOURCE, bars, NQ, data2_closes=data2_closes
    )["records"]
    native_fills = run_native(DATA2_SOURCE, bars, NQ, data2_bars=data2_tape)

    assert trade_difference(python_records, native_fills) is None
    assert fills_sha256(native_fills) == fills_sha256(python_records)


def test_python_data2_bars_matches_native_data2_bars():
    """Python path given only data2_bars must match the native path.

    The silent-ignore bug made python(data2_bars=) return the Data1 fallback
    while native(data2_bars=) returned the audit. Both spellings, both paths,
    one digest.
    """
    _require_accel()
    bars = _sample_bars()
    data2_tape = [dict(b, close=b["close"] + 3.0) for b in bars]

    python = run_oracle(DATA2_SOURCE, bars, NQ, data2_bars=data2_tape)
    native = run_oracle(
        DATA2_SOURCE, bars, NQ, data2_bars=data2_tape, use_native=True,
    )
    assert python["native"] is False
    assert native["native"] is True
    assert python["text"] == native["text"]
    assert python["sha256"] == native["sha256"]
    assert python["fills"] == native["fills"]


def test_refusal_is_loud_and_python_still_runs():
    _require_accel()
    bars = _sample_bars()
    # Close of Data2 with no Data2 supplied: the kernel refuses loudly; the
    # Python path falls back to Close of Data1 and still produces a digest.
    with pytest.raises(AccelRefused):
        run_native(DATA2_SOURCE, bars, NQ)

    python_digest = run_oracle(DATA2_SOURCE, bars, NQ)
    assert python_digest["native"] is False
    assert python_digest["text"].startswith("powerbridge-trade-digest v1\n")


def test_run_oracle_forwards_data2_on_native_path():
    _require_accel()
    import pl_transpiler._accel as accel

    bars = _sample_bars()
    data2_tape = [dict(b, close=b["close"] + 3.0) for b in bars]
    data2_closes = [b["close"] for b in data2_tape]

    # Spy the FFI crossing so the test can prove the Data2 tape was handed
    # across, and that the native path (not a silent fallback) produced the
    # digest.
    real_run_program = accel.run_program
    calls = []

    def spy(program_ir, series, data2, config):
        calls.append((program_ir, series, data2, config))
        return real_run_program(program_ir, series, data2, config)

    accel.run_program = spy
    try:
        result = run_oracle(
            DATA2_SOURCE, bars, NQ,
            data2_closes=data2_closes, data2_bars=data2_tape,
            use_native=True,
        )
        python = run_oracle(DATA2_SOURCE, bars, NQ, data2_closes=data2_closes)
    finally:
        accel.run_program = real_run_program

    assert result["native"] is True, (
        "run_oracle(use_native=True, data2_bars=…) must take the native path, "
        "not silently fall back"
    )
    assert len(calls) == 1
    data2 = calls[0][2]
    assert data2 is not None and data2["date"] == [b["date"] for b in data2_tape]
    assert data2["close"] == [b["close"] for b in data2_tape]
    assert result["text"] == python["text"]


def test_run_oracle_use_native_digest_identical():
    _require_accel()
    gt2 = open(GT2, encoding="utf-8").read()
    bars = _sample_bars()

    native = run_oracle(gt2, bars, NQ, use_native=True)
    python = run_oracle(gt2, bars, NQ, use_native=False)

    assert native["native"] is True
    assert python["native"] is False
    assert native["text"] == python["text"]


def test_run_oracle_use_native_falls_back_loudly_on_refusal(capsys):
    _require_accel()
    bars = _sample_bars()
    # Native refuses (Data2 without a Data2 tape); use_native must fall back to
    # the Python path loudly rather than returning a partial trade list.
    result = run_oracle(DATA2_SOURCE, bars, NQ, use_native=True)

    assert result["native"] is False
    assert result["native_fallback_reason"].startswith("native path refused:")
    stderr = capsys.readouterr().err
    assert "native path refused" in stderr
    assert "falling back to the Python path" in stderr
    # The fallback digest agrees with a pure-Python run (identical trade list).
    assert result["text"] == run_oracle(DATA2_SOURCE, bars, NQ)["text"]


def test_unavailable_is_loud_not_a_trade_list(monkeypatch):
    # Even without the extension built, run_native must raise AccelUnavailable
    # rather than fabricate trades. Force the `import pl_transpiler._accel` to
    # fail so _import_accel's own except ImportError wraps it loudly.
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pl_transpiler._accel":
            raise ImportError("no module named 'pl_transpiler._accel'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    from pl_transpiler.runtime.accel import AccelUnavailable

    with pytest.raises(AccelUnavailable):
        run_native("If Close > 0 Then Buy Next Bar At Market;",
                   _sample_bars(), NQ)
