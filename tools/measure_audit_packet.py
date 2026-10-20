#!/usr/bin/env python3
"""Measure production native loop with vs without audit-event-v1 collection.

protocol: fixture (fix-mid) — never a programme number.
Interleaved A/B, >=3 reps, median + spread + loadavg.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pl_transpiler.runtime.accel import (  # noqa: E402
    _config_dict,
    _serialise_and_marshal,
    fallback_count,
)
from pl_transpiler.runtime.audit_packet import (  # noqa: E402
    decode,
    write_trades_csv,
)
from pl_transpiler.runtime.instrument_config import get_config  # noqa: E402
from pl_transpiler.tools.pl_oracle import fills_sha256  # noqa: E402
from pl_transpiler.tools.pl_run import load_bars_csv  # noqa: E402

NQ = get_config("NQ")
GOLDEN_TRADES = "3f52271ba2c8ed2ede83413a466046a9da9c46a072ec38a96c28425a4de808f8"


def loadavg() -> str:
    try:
        return " ".join(f"{x:.2f}" for x in os.getloadavg())
    except OSError:
        return "unavailable"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def median_spread(values: list[float]) -> tuple[float, float]:
    med = statistics.median(values)
    return med, max(values) - min(values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--el", required=True)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--data2", required=True)
    parser.add_argument("--golden-trades", default="")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--out-dir", default="/tmp/report-43-audit")
    args = parser.parse_args()

    el = Path(args.el).read_text(encoding="utf-8")
    print(f"[measure] loading bars {args.bars}", flush=True)
    t0 = time.perf_counter()
    bars = load_bars_csv(args.bars)
    data2 = load_bars_csv(args.data2)
    print(
        f"[measure] loaded {len(bars)} / {len(data2)} bars in {time.perf_counter() - t0:.3f}s",
        flush=True,
    )
    print(f"[measure] loadavg {loadavg()}", flush=True)

    print("[measure] serialise IR + marshal tapes (untimed)", flush=True)
    t0 = time.perf_counter()
    accel, program_ir, series, data2_series = _serialise_and_marshal(el, bars, data2)
    config_map = _config_dict(NQ)
    print(f"[measure] serialise+marshal {time.perf_counter() - t0:.3f}s", flush=True)

    print("[measure] warmup without", flush=True)
    accel.run_program(program_ir, series, data2_series, config_map)
    print("[measure] warmup with", flush=True)
    accel.run_program_audit_packet(
        program_ir, series, data2_series, config_map, False
    )
    fallback_before = fallback_count()

    without: list[float] = []
    with_packet: list[float] = []
    last_fills = None
    last_packet = None
    for i in range(1, args.reps + 1):
        t0 = time.perf_counter()
        fills = accel.run_program(program_ir, series, data2_series, config_map)
        without.append(time.perf_counter() - t0)
        last_fills = fills
        print(f"[measure] without {i}/{args.reps} {without[-1]:.6f}s loadavg {loadavg()}", flush=True)

        t0 = time.perf_counter()
        packet = accel.run_program_audit_packet(
            program_ir, series, data2_series, config_map, False
        )
        with_packet.append(time.perf_counter() - t0)
        last_packet = packet
        print(
            f"[measure] with    {i}/{args.reps} {with_packet[-1]:.6f}s "
            f"bytes={len(packet)} loadavg {loadavg()}",
            flush=True,
        )

    if fallback_count() != fallback_before:
        print("[measure] FAIL fallback counter moved", file=sys.stderr)
        return 2

    decoded = decode(last_packet)
    header = decoded["header"]
    fill_digest = fills_sha256(last_fills)
    print(f"[measure] fills {len(last_fills)} digest {fill_digest}")
    print(
        f"[measure] packet fills {header['fill_count']} round_turns {header['round_turn_count']} "
        f"transitions {header['transition_count']} trades {header['trade_count']} "
        f"eval_from {header['eval_from']} remaining_open {header['remaining_open']} "
        f"hash_fills {header['hash_fills']}"
    )
    fills_match = header["hash_fills"] == fill_digest
    print(f"[measure] fill-digest with==without {fills_match}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    packet_path = out_dir / "audit-event-v1.bin"
    packet_path.write_bytes(last_packet)
    trades_path = out_dir / "trades.csv"
    if header["remaining_open"] != 0:
        print(f"[measure] remaining_open={header['remaining_open']}; not writing trades.csv")
        trades_sha = ""
        golden_match = False
    else:
        write_trades_csv(trades_path, decoded["trades"])
        trades_sha = sha256_file(trades_path)
        print(f"[measure] trades.csv sha256 {trades_sha} lines {sum(1 for _ in trades_path.open())}")
        golden_match = trades_sha == GOLDEN_TRADES
        print(f"[measure] trades.csv vs full-tape golden {golden_match}")
        if args.golden_trades:
            golden_path = Path(args.golden_trades)
            same = trades_path.read_bytes() == golden_path.read_bytes()
            print(f"[measure] cmp vs {golden_path} {same}")

    w_med, w_spread = median_spread(without)
    p_med, p_spread = median_spread(with_packet)
    delta = p_med - w_med
    noise = 0.13 * max(w_med, p_med)
    scored = 0.0 if abs(delta) <= noise else delta
    label = "0.0 (noise)" if scored == 0.0 else f"{scored:.6f}"
    print(
        f"[measure] protocol: fixture (fix-mid)\n"
        f"[measure] without median {w_med:.6f}s spread {w_spread:.6f} values {without}\n"
        f"[measure] with    median {p_med:.6f}s spread {p_spread:.6f} values {with_packet}\n"
        f"[measure] delta(with-without) {delta:.6f}s noise_band {noise:.6f}s scored {label}\n"
        f"[measure] loadavg {loadavg()} packet_bytes {len(last_packet)}"
    )
    summary = out_dir / "measure.txt"
    summary.write_text(
        f"protocol: fixture (fix-mid)\n"
        f"without: {without}\n"
        f"with: {with_packet}\n"
        f"without_median: {w_med:.6f}\n"
        f"with_median: {p_med:.6f}\n"
        f"delta: {delta:.6f}\n"
        f"scored: {label}\n"
        f"fills: {len(last_fills)}\n"
        f"fill_digest: {fill_digest}\n"
        f"packet_fill_digest: {header['hash_fills']}\n"
        f"fill_digest_match: {fills_match}\n"
        f"transitions: {header['transition_count']}\n"
        f"trades: {header['trade_count']}\n"
        f"eval_from: {header['eval_from']}\n"
        f"remaining_open: {header['remaining_open']}\n"
        f"packet_bytes: {len(last_packet)}\n"
        f"trades_sha256: {trades_sha}\n"
        f"golden_match: {golden_match}\n"
        f"fallback_moved: {fallback_count() != fallback_before}\n"
        f"loadavg: {loadavg()}\n",
        encoding="utf-8",
    )
    return 0 if fills_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
