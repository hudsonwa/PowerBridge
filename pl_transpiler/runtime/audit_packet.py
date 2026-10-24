# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# See the License for the specific language governing permissions
# and limitations under the License.

"""Decode a framed audit-event-v1 packet and format legacy trades.csv.

Python owns legacy text rendering. The native collector never emits per-bar
strings. Schema item 6 B: TransitionEvent is 104 B and does not
carry `priorsessionweightedmean` or `c3entrylevel`.
"""
from __future__ import annotations

import csv
import hashlib
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

MAGIC = b"AUEVT1\0\0"
VERSION = 1
HEADER_SIZE = 256
TRANSITION_SIZE = 104
TRADE_SIZE = 88

HEADER_STRUCT = struct.Struct("<8sHHIIIIHHiIdIIII")
TRANSITION_STRUCT = struct.Struct("<IBBbxiIi4x10d")
TRADE_STRUCT = struct.Struct("<IIiiHHBBH8d")

assert HEADER_STRUCT.size == 64
assert TRANSITION_STRUCT.size == TRANSITION_SIZE
assert TRADE_STRUCT.size == TRADE_SIZE

BOOL_FIELDS = (
    "historyready",
    "c1gate",
    "entrygate",
    "emergencyexit",
    "eodexit",
)
FLOAT_FIELDS = (
    "entry_price",
    "net_profit",
    "priorsessionclose",
    "priorsessionunadjustedclose",
    "priorsessiontr",
    "c3scaledlevel",
    "c4cappedrange",
    "c4traildistance",
    "bestclosesincesignal",
    "trailstop",
)

# 24-col TRACE_COLUMNS names this packet does not carry. Requesting any of
# these must raise (GATE-REDUCED-TRACE invariants 2 and 5). Never substitute 0.
PACKET_DOES_NOT_CARRY = frozenset(
    {
        "_date",
        "_time",
        "_close",
        "priorsessionweightedmean",
        "c3entrylevel",
    }
)

# TRACE_COLUMNS names the packet *does* carry (engine-owned + live slots).
# `_date`/`_time`/`_close` are recovered from the digest-bound Data1 tape, not
# stored. `_market_position` is `market_position` in the record.
PACKET_CARRIED_TRACE_COLUMNS = frozenset(
    {
        "_market_position",
        "current_contracts",
        "entry_price",
        "net_profit",
        "totaltrades",
        "priorsessionclose",
        "priorsessionunadjustedclose",
        "priorsessiontr",
        "c3scaledlevel",
        "c4cappedrange",
        "c4traildistance",
        "historyready",
        "c1gate",
        "entrygate",
        "ladderbarselapsed",
        "bestclosesincesignal",
        "trailstop",
        "emergencyexit",
        "eodexit",
    }
)

TRADE_CSV_FIELDS = (
    "entry_datetime",
    "entry_price",
    "exit_datetime",
    "exit_price",
    "net_profit",
    "bars_held",
    "true_range",
    "true_close",
    "yesterday_close",
)


def refuse_missing_columns(columns: list[str]) -> None:
    """Raise if a caller asked for a column the packet does not carry.

    A quiet ``0`` in a trace cell is indistinguishable from a real zero.
    """
    missing = [name for name in columns if name not in PACKET_CARRIED_TRACE_COLUMNS]
    if missing:
        raise ValueError(
            "audit-event-v1 packet does not carry columns "
            f"{missing}; refusing silent default"
        )


def looks_like_packet(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(8) == MAGIC


def datetime_from_el(el_date: int, el_time: int) -> datetime:
    return datetime(
        1900 + el_date // 10000,
        (el_date // 100) % 100,
        el_date % 100,
        el_time // 100,
        el_time % 100,
    )


def parse_header(packet: bytes) -> dict[str, Any]:
    if len(packet) < HEADER_SIZE:
        raise ValueError(f"audit-event-v1 packet shorter than header ({len(packet)})")
    header = packet[:HEADER_SIZE]
    (
        magic,
        version,
        header_size,
        flags,
        source_bar_count,
        transition_count,
        trade_count,
        trans_size,
        trade_size,
        ending_contracts,
        closed_campaign_count,
        engine_gross_net,
        fill_count,
        round_turn_count,
        eval_from,
        remaining_open,
    ) = HEADER_STRUCT.unpack(header[:64])
    if magic != MAGIC:
        raise ValueError(f"bad audit-event-v1 magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported audit-event-v1 version {version}")
    if header_size != HEADER_SIZE:
        raise ValueError(f"unexpected header size {header_size}")
    if trans_size != TRANSITION_SIZE or trade_size != TRADE_SIZE:
        raise ValueError(f"unexpected record sizes {trans_size}/{trade_size}")
    expected = HEADER_SIZE + transition_count * TRANSITION_SIZE + trade_count * TRADE_SIZE
    if len(packet) != expected:
        raise ValueError(f"packet length {len(packet)} != {expected}")
    return {
        "flags": flags,
        "source_bar_count": source_bar_count,
        "transition_count": transition_count,
        "trade_count": trade_count,
        "ending_contracts": ending_contracts,
        "closed_campaign_count": closed_campaign_count,
        "engine_gross_net_profit": engine_gross_net,
        "fill_count": fill_count,
        "round_turn_count": round_turn_count,
        "eval_from": eval_from,
        "remaining_open": remaining_open,
        "hash_data1": header[64:96].hex(),
        "hash_data2": header[96:128].hex(),
        "hash_ir": header[128:160].hex(),
        "hash_config": header[160:192].hex(),
        "hash_fills": header[192:224].hex(),
        "hash_records": header[224:256].hex(),
    }


def parse_transitions(packet: bytes, header: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    offset = HEADER_SIZE
    for _ in range(header["transition_count"]):
        raw = packet[offset : offset + TRANSITION_SIZE]
        offset += TRANSITION_SIZE
        fields = TRANSITION_STRUCT.unpack(raw)
        bar_index, kind, mask, market_position, contracts, totaltrades, ladder = fields[:7]
        floats = fields[7:]
        row = {
            "bar_index": bar_index,
            "event_kind": kind,
            "market_position": market_position,
            "current_contracts": contracts,
            "totaltrades": totaltrades,
            "ladderbarselapsed": ladder,
        }
        for i, name in enumerate(BOOL_FIELDS):
            row[name] = bool(mask & (1 << i))
        for name, value in zip(FLOAT_FIELDS, floats):
            row[name] = value
        out.append(row)
    return out


def parse_trades(packet: bytes, header: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    offset = HEADER_SIZE + header["transition_count"] * TRANSITION_SIZE
    for _ in range(header["trade_count"]):
        raw = packet[offset : offset + TRADE_SIZE]
        offset += TRADE_SIZE
        (
            entry_bar,
            exit_bar,
            entry_date,
            exit_date,
            entry_time,
            exit_time,
            direction,
            reason,
            contracts,
            entry_price,
            exit_price,
            net_after_cost,
            true_range,
            true_close,
            yesterday_close,
            cumulative,
            drawdown,
        ) = TRADE_STRUCT.unpack(raw)
        out.append(
            {
                "entry_bar": entry_bar,
                "exit_bar": exit_bar,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "direction": direction,
                "reason": reason,
                "contracts": contracts,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "net_profit": net_after_cost,
                "true_range": true_range,
                "true_close": true_close,
                "yesterday_close": yesterday_close,
                "cumulative_net": cumulative,
                "drawdown": drawdown,
                "entry_datetime": datetime_from_el(entry_date, entry_time),
                "exit_datetime": datetime_from_el(exit_date, exit_time),
                "bars_held": exit_bar - entry_bar,
            }
        )
    return out


def decode(packet: bytes) -> dict[str, Any]:
    header = parse_header(packet)
    records = packet[HEADER_SIZE:]
    digest = hashlib.sha256(records).hexdigest()
    if digest != header["hash_records"]:
        raise ValueError("records SHA-256 does not match header")
    return {
        "header": header,
        "transitions": parse_transitions(packet, header),
        "trades": parse_trades(packet, header),
    }


def require_trade_fields(row: dict[str, Any]) -> None:
    missing = [name for name in TRADE_CSV_FIELDS if name not in row]
    if missing:
        raise ValueError(
            "audit-event-v1 trade record missing fields "
            f"{missing}; refusing silent default"
        )


def report_trades(decoded: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Trades + trace_meta for the NQ report build. Raises on open contracts."""
    header = decoded["header"]
    remaining = header["remaining_open"]
    if remaining:
        raise ValueError(
            f"audit-event-v1 packet ends with {remaining} open contracts"
        )
    trades = decoded["trades"]
    if len(trades) != header["trade_count"]:
        raise ValueError(
            f"packet trade_count {header['trade_count']} != {len(trades)} decoded"
        )
    for row in trades:
        require_trade_fields(row)
    trace_meta = {
        "trace_rows": header["source_bar_count"],
        "ending_contracts": header["ending_contracts"],
        "engine_gross_net_profit": header["engine_gross_net_profit"],
        "engine_total_positions": header["closed_campaign_count"],
    }
    return trades, trace_meta


def write_trades_csv(path: Path, trades: list[dict[str, Any]]) -> None:
    headers = [
        "Position",
        "Symbol",
        "Contracts",
        "Entry Date",
        "Entry Price",
        "Exit Date",
        "Exit Price",
        "Net Profit",
        "Bars Held",
        "True Range",
        "true close",
        "Yesterday Close",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        for row in trades:
            require_trade_fields(row)
            writer.writerow(
                [
                    "Long",
                    "NQM",
                    1,
                    row["entry_datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                    row["entry_price"],
                    row["exit_datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                    row["exit_price"],
                    row["net_profit"],
                    row["bars_held"],
                    row["true_range"],
                    row["true_close"],
                    row["yesterday_close"],
                ]
            )


def write_equity_csv(path: Path, trades: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["Trade Number", "Exit Date", "Trade Net Profit", "Cumulative Net Profit", "Drawdown"]
        )
        for number, row in enumerate(trades, 1):
            require_trade_fields(row)
            if "cumulative_net" not in row or "drawdown" not in row:
                raise ValueError(
                    "audit-event-v1 trade record missing equity fields "
                    "cumulative_net/drawdown; refusing silent default"
                )
            writer.writerow(
                [
                    number,
                    row["exit_datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                    row["net_profit"],
                    row["cumulative_net"],
                    row["drawdown"],
                ]
            )
