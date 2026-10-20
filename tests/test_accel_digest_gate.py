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

"""The native-vs-Python canonical-digest differential gate (issue #5).

One test: the native kernel and the Python path must produce the identical
canonical digest for the same program over the same tape, AND the native path
must actually have been taken.

The false-green this test exists to prevent: a digest that silently came from
the Python fallback. ``run_oracle(use_native=True)`` can fall back to Python and
still return a digest — comparing two such digests is trivially green while
proving nothing about the native kernel. Here the native fills come straight
from ``run_native`` (which raises ``AccelUnavailable`` / ``AccelRefused`` rather
than fall back), and the fallback counter is asserted unchanged, so a fallback
surfaces as a failure, never a pass.

Skips cleanly (no native extension) so a captures-less / no-Rust clone stays
green: the skip is the documented "extra absent" state, never a pass.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import (  # noqa: E402
    accel_available,
    fallback_count,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import (  # noqa: E402
    fills_sha256,
    format_digest,
    run_oracle,
)
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

NQ = get_config("NQ")
SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
GT2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")


def test_native_and_python_digest_identical_with_native_taken():
    if not accel_available():
        pytest.skip("pl_transpiler._accel is not installed (native path absent)")

    el_source = open(GT2, encoding="utf-8").read()
    bars = load_bars_csv(SAMPLES)

    # The native fills must come from the kernel, not a Python fallback. If the
    # extension is present but the kernel refuses the program, run_native raises
    # AccelRefused and this test fails (it must NOT be turned into a skip/pass).
    fallbacks_before = fallback_count()
    native_fills = run_native(el_source, bars, NQ)
    assert fallback_count() == fallbacks_before, (
        "a native-path fallback fired during the run; the digest would be a "
        "Python fallback digest (false green)"
    )

    python_digest = run_oracle(el_source, bars, NQ)

    native_sha = fills_sha256(native_fills)
    assert native_sha == python_digest["sha256"], "native/Python fill sha256 differs"

    native_digest_text = format_digest(
        fills=len(native_fills),
        round_turns=len(native_fills) // 2,
        sha256=native_sha,
    )
    assert native_digest_text == python_digest["text"], (
        "native/Python canonical digest text differs"
    )
