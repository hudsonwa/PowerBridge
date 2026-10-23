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

"""Identity cache for the FFI ``series`` dicts.

Boxing ~7 columns of a tape into Python lists is the marshal cost. A sweep
of N variants over the same tape must marshal once, not N times. These tests
prove the cache hits on tape identity, misses on a different tape object
(never a stale dict), and that config is not part of the key.

Stdlib only; does not require ``pl_transpiler._accel``.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest

from pl_transpiler.runtime.accel import (  # noqa: E402
    _MAX_CACHED_TAPES,
    _series_dict,
    accel_available,
    clear_series_cache,
    marshal_count,
    marshalled_series,
    run_native,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402
from tests.skip_messages import ACCEL_SKIP  # noqa: E402

NQ = get_config("NQ")

SAMPLES = os.path.join(REPO, "examples", "NQ_sample_bars.csv")


def _bars():
    return load_bars_csv(SAMPLES)


def setup_function(_fn):
    clear_series_cache()


def test_repeat_over_same_tape_does_not_remarshal():
    bars = _bars()
    before = marshal_count()
    series1, data2_1 = marshalled_series(bars)
    after_first = marshal_count()
    series2, data2_2 = marshalled_series(bars)
    after_second = marshal_count()

    assert after_first == before + 1
    assert after_second == after_first, (
        "repeat marshalled_series over the same tape object must be a hit"
    )
    assert series1 is series2
    assert data2_1 is None and data2_2 is None
    assert list(series1) == ["open", "high", "low", "close", "volume", "date", "time"]
    assert series1 == _series_dict(bars)


def test_different_tape_object_is_a_miss_never_stale():
    bars_a = _bars()
    bars_b = _bars()
    assert bars_a is not bars_b
    # Same contents, different identity: must marshal again, and must not
    # return the dict built for bars_a (a stale-tape leak would pass a
    # content-equal check here if we mutated after the first marshal).
    series_a, _ = marshalled_series(bars_a)
    before = marshal_count()
    bars_b[0] = dict(bars_b[0], close=bars_b[0]["close"] + 99.0)
    series_b, _ = marshalled_series(bars_b)

    assert marshal_count() == before + 1
    assert series_a is not series_b
    assert series_b["close"][0] == bars_b[0]["close"]
    assert series_a["close"][0] != series_b["close"][0]


def test_same_bars_different_data2_does_not_reuse_data2():
    bars = _bars()
    data2_a = [dict(b, close=b["close"] + 1.0) for b in bars]
    data2_b = [dict(b, close=b["close"] + 2.0) for b in bars]

    series_1, d2_1 = marshalled_series(bars, data2_a)
    before = marshal_count()
    series_2, d2_2 = marshalled_series(bars, data2_b)
    after = marshal_count()

    assert series_1 is series_2, "primary tape identity is unchanged"
    assert after == before + 1, "only the new data2 tape is marshalled"
    assert d2_1 is not d2_2
    assert d2_1["close"][0] == data2_a[0]["close"]
    assert d2_2["close"][0] == data2_b[0]["close"]


def test_data2_none_then_supplied_is_not_stale_none():
    bars = _bars()
    data2 = [dict(b) for b in bars]
    series_1, d2_1 = marshalled_series(bars, None)
    series_2, d2_2 = marshalled_series(bars, data2)
    assert series_1 is series_2
    assert d2_1 is None
    assert d2_2 is not None
    assert d2_2["date"] == [b["date"] for b in data2]


def test_cache_eviction_does_not_return_stale_id_reuse():
    """A recycled CPython id must miss: the cache verifies with ``is``."""
    bars = _bars()
    series_first, _ = marshalled_series(bars)
    first_id = id(bars)

    extras = [_bars() for _ in range(_MAX_CACHED_TAPES)]
    for extra in extras:
        marshalled_series(extra)
    # ``bars`` is now evicted. A new tape that happens to occupy the same
    # id() slot (we cannot force that) still must not return series_first
    # because the ``is`` check fails. Prove the miss path with a new object.
    new_bars = _bars()
    before = marshal_count()
    series_new, _ = marshalled_series(new_bars)
    assert marshal_count() == before + 1
    assert series_new is not series_first
    assert id(new_bars) != first_id or new_bars is not bars


def test_clear_series_cache_forces_remarshal():
    bars = _bars()
    series_1, _ = marshalled_series(bars)
    clear_series_cache()
    before = marshal_count()
    series_2, _ = marshalled_series(bars)
    assert marshal_count() == before + 1
    assert series_1 is not series_2
    assert series_1 == series_2


@pytest.mark.needs_native
def test_run_native_repeat_does_not_remarshal():
    """The caller cache is on the tape, not the config: two runs, one marshal."""
    if not accel_available():
        pytest.skip(ACCEL_SKIP)
    gt2 = os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt")
    source = open(gt2, encoding="utf-8").read()
    bars = _bars()
    clear_series_cache()
    before = marshal_count()
    fills_a = run_native(source, bars, NQ)
    after_first = marshal_count()
    fills_b = run_native(source, bars, NQ)
    after_second = marshal_count()
    assert after_first == before + 1
    assert after_second == after_first
    assert fills_a == fills_b
