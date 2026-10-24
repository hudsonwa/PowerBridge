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
"""Time the Python path against the Rust kernel on a synthetic tape.

    python tools/bench_native.py --bars 20000 --reps 3

Builds a deterministic random-walk tape from examples/NQ_sample_bars.csv,
runs the GT2 sample strategy on both paths, checks the fills are identical,
and prints the median times. Needs the accel module (see README).
"""
import argparse
import os
import random
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import accel_available, run_native  # noqa: E402
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv, run_el  # noqa: E402

FIELDS = ("type", "signal", "date", "time", "price", "contracts")


def synthetic_tape(n_bars, seed=1):
    template = load_bars_csv(os.path.join(REPO, "examples", "NQ_sample_bars.csv"))[0]
    rng = random.Random(seed)
    price = 15000.0
    bars = []
    for i in range(n_bars):
        bar = dict(template)
        open_ = price
        price = round((price + rng.gauss(0, 4)) * 4) / 4
        minute = i % 390
        bar.update(open=open_, high=max(open_, price) + 1, low=min(open_, price) - 1,
                   close=price, date=1240102 + i // 390,
                   time=930 + minute // 60 * 100 + minute % 60)
        bars.append(bar)
    return bars


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args(argv)
    if not accel_available():
        print("bench_native: the accel module is not installed (see README)", file=sys.stderr)
        return 2
    source = open(os.path.join(REPO, "ground_truth", "GT2_strategy_orders_position.txt"),
                  encoding="utf-8").read()
    config = get_config("NQ")
    bars = synthetic_tape(args.bars)
    py_times, rs_times = [], []
    for _ in range(args.reps):
        t0 = time.perf_counter()
        python_fills = run_el(source, bars, config, max_bars_back=0)["trades"]
        py_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        native_fills = run_native(source, bars, config)
        rs_times.append(time.perf_counter() - t0)
    same = [{k: f[k] for k in FIELDS} for f in python_fills] == \
           [{k: f[k] for k in FIELDS} for f in native_fills]
    py, rs = statistics.median(py_times), statistics.median(rs_times)
    print(f"{args.bars} bars, {len(native_fills)} fills, identical={same}")
    print(f"python {py:.3f}s  rust {rs:.3f}s  speedup {py / rs:.1f}x  (median of {args.reps})")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
