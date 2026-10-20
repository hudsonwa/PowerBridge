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

"""
test_f32.py — proves pl_runtime.f32 is bit-identical to the historical
struct.pack/unpack float32 round-trip.

f32 exists to emulate EasyLanguage single precision. A faster implementation is
only legal if every output bit matches the struct oracle, including None, ±inf,
signed nan, -0.0, overflow, subnormals, bools, and numeric strings.

Exit 0 iff all groups pass.
"""
import math
import os
import random
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.pl_runtime import f32 as live_f32


def _oracle(x):
    """Frozen historical implementation (struct round-trip)."""
    if x is None:
        return 0.0
    try:
        return struct.unpack('f', struct.pack('f', float(x)))[0]
    except OverflowError:
        # Python >= 3.13 raises here for a finite value beyond the float32 range
        # (older versions returned +-inf). IEEE-754 round-to-nearest gives +-inf.
        return math.copysign(math.inf, float(x))
    except (struct.error, ValueError, TypeError):
        return float(x)


def _call(fn, x):
    try:
        return ('ok', fn(x))
    except Exception as e:
        return ('err', type(e).__name__)


def _same_float(a, b):
    """Bit-identical IEEE-754 binary64 payload (so nan and -0.0 compare)."""
    return type(a) is float and type(b) is float and struct.pack('d', a) == struct.pack('d', b)


def _assert_match(x, where):
    got = _call(live_f32, x)
    want = _call(_oracle, x)
    if got[0] != want[0]:
        raise AssertionError(
            f"{where}: {x!r} status live={got} oracle={want}")
    if got[0] == 'err':
        if got[1] != want[1]:
            raise AssertionError(
                f"{where}: {x!r} exception live={got[1]} oracle={want[1]}")
        return
    if not _same_float(got[1], want[1]):
        raise AssertionError(
            f"{where}: {x!r} bits live={got[1]!r} ({struct.pack('d', got[1]).hex()}) "
            f"oracle={want[1]!r} ({struct.pack('d', want[1]).hex()})")


def test_f32_edge_cases():
    samples = [
        None, 0, 0.0, -0.0, 1, -1, 1.0, -1.0, True, False,
        0.1, 0.2, 0.3, math.pi, math.e,
        1e-45, 1.4e-45, 1e-40, 1.17549435e-38,
        3.4028234663852886e+38, 3.4028235e38, 3.4028236e38,
        1e38, 3e38, 1e39, 1e40, 1e308, -1e40, -3.4028235e38,
        float('inf'), float('-inf'), float('nan'), -float('nan'),
        '0.1', '0.2', '1.5', '0', '-3.25', 'nan', 'inf', '-inf',
        2**10, 2**20, 2**40, 2**60,
    ]
    for i, v in enumerate(samples):
        _assert_match(v, f"edge[{i}]")
    print(f"  [ok] {len(samples)} edge cases bit-identical to struct oracle")


def test_f32_random_sample():
    random.seed(7)
    n = 100000
    for i in range(n):
        _assert_match(random.uniform(-1e4, 1e4), f"uniform[{i}]")
    n_log = 20000
    for i in range(n_log):
        mag = 10 ** random.uniform(-45, 40)
        v = mag if random.random() < 0.5 else -mag
        _assert_match(v, f"logmag[{i}]")
    print(f"  [ok] {n} uniform + {n_log} log-magnitude samples bit-identical")


def test_f32_exception_parity():
    for v in (b'nope', object(), [], {}, complex(1, 0), 10**400):
        _assert_match(v, f"exc[{v!r}]")
    print("  [ok] exception parity vs struct oracle")


def test_f32_none_and_signed_zero():
    assert live_f32(None) == 0.0
    assert math.copysign(1.0, live_f32(-0.0)) == -1.0
    assert math.copysign(1.0, live_f32(0.0)) == 1.0
    print("  [ok] None -> 0.0 and signed zero preserved")


def main():
    tests = [
        test_f32_edge_cases,
        test_f32_random_sample,
        test_f32_exception_parity,
        test_f32_none_and_signed_zero,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL F32 BIT-IDENTITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
