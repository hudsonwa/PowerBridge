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
instrument_config.py — per-run chart/instrument configuration.

The pure-Python runner/runtime must be instrument-agnostic: the ~dozen
chart/instrument constants that make a run NQ-specific (point value, tick,
min-move, price scale, session start, bar interval, and the @NQ#C
instrument-dictionary properties) belong in a PER-RUN config object, NOT baked
into the shared runtime (transpiler/runtime/pl_runtime.py) or hard-coded in
tests/run_gt.py.

This module holds:
  * InstrumentConfig — an immutable dataclass of those constants.
  * NQ — the config reproducing today's exact validated @NQ#C values.
  * ES — @ES#C RTH 1-min.
  * get_config(name) — select a config by name, or load it from a JSON file.
  * load_config_file(path) — read an InstrumentConfig from a JSON file whose
    keys are exactly the InstrumentConfig fields (examples/configs/*.json).

Integrity note: these are KNOWN chart/instrument config (sourced from the chart
spec, not from the capture under test), so injecting them per-run preserves the
audited non-circular sourcing.
"""
import json
import math
import os
from dataclasses import MISSING, dataclass, fields
from typing import Any, Union


@dataclass(frozen=True)
class InstrumentConfig:
    """Chart/instrument constants for one run. All fields are KNOWN chart config,
    never sourced from the capture being verified."""
    name: str
    # Contract spec (drives fill PnL: $/point = bigpointvalue per contract).
    bigpointvalue: float   # $ per full point per contract (NQ = 20)
    pointvalue: float      # $ per minimum price move (NQ = 0.20)
    minmove: int           # number of minmove units per point (NQ = 25)
    pricescale: int        # price scale (NQ = 100)
    tick: float            # minimum price increment in points (NQ = 0.25)
    # Session / bar config.
    sess1starttime: int    # session 1 start, EL HHmm (NQ = 830)
    barinterval: int       # bar interval (1-min = 1)
    # Instrument-dictionary properties (static @NQ#C dictionary fields).
    symccy: int            # SymbolCurrency code
    symnm: str             # Symbol name
    symalt: str            # Alternate symbol name
    descstr: str           # Description string
    expdate: int           # Expiration date (YYYMMdd EL form)
    # Category code (MultiCharts/TradeStation numbering: 0 = future, 1 = future
    # option, 2 = stock, 4 = index, 12 = forex, ...).
    category: int = 0
    # Round-turn trading cost in $ per contract (commission plus slippage for
    # one entry and its exit). The native audit packet subtracts it from each
    # closed trade's gross profit to give net profit. Fills are not affected.
    round_turn_cost: float = 25.0


# The NQ config holds the validated @NQ#C instrument values. Selected for all
# current GT labels, which are all @NQ#C captures.
NQ = InstrumentConfig(
    name="NQ",
    bigpointvalue=20,
    pointvalue=0.20,
    minmove=25,
    pricescale=100,
    tick=0.25,
    sess1starttime=830,
    barinterval=1,
    symccy=1,
    symnm="@NQ#C",
    symalt="@NQ#C",
    descstr="E-MINI NASDAQ 100",
    expdate=1130331,
    round_turn_cost=25.0,
)


# ES config for @ES#C RTH 1-min. Native fill engine consumes
# bigpointvalue + tick; other fields mirror NQ session shape with ES symbols.
# pointvalue = bigpointvalue / pricescale (same relation as NQ: 20/100=0.20).
ES = InstrumentConfig(
    name="ES",
    bigpointvalue=50,
    pointvalue=0.50,
    minmove=25,
    pricescale=100,
    tick=0.25,
    sess1starttime=830,
    barinterval=1,
    symccy=1,
    symnm="@ES#C",
    symalt="@ES#C",
    descstr="E-MINI S&P 500",
    expdate=1130331,
    round_turn_cost=25.0,
)


_CONFIGS: dict[str, InstrumentConfig] = {
    "NQ": NQ,
    "ES": ES,
}


class ConfigFileError(ValueError):
    """A malformed instrument config JSON file (one-line message)."""


def _type_ok(value: Any, typ: Any) -> bool:
    # bool is an int subclass but never a valid numeric config value.
    if isinstance(value, bool):
        return False
    if typ is float:
        return isinstance(value, (int, float))
    return isinstance(value, typ)


def load_config_file(path: Union[str, "os.PathLike[str]"]) -> InstrumentConfig:
    """Load an InstrumentConfig from the JSON object in `path`.

    Keys are exactly the InstrumentConfig field names; `category` (default 0)
    and `round_turn_cost` (default 25.0, must be >= 0) are optional. Unknown
    keys, missing keys, wrong types and a negative `round_turn_cost` raise
    ConfigFileError; a missing file raises OSError.
    """
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except ValueError as exc:
            raise ConfigFileError(f"bad config file {path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigFileError(f"bad config file {path}: expected a JSON object")
    spec = {f.name: f for f in fields(InstrumentConfig)}
    unknown = sorted(set(data) - set(spec))
    if unknown:
        raise ConfigFileError(
            f"bad config file {path}: unknown key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(spec)}")
    missing = [n for n, f in spec.items() if n not in data and f.default is MISSING]
    if missing:
        raise ConfigFileError(
            f"bad config file {path}: missing key(s) {', '.join(missing)}")
    for name, value in data.items():
        typ: Any = spec[name].type
        typ = {"float": float, "int": int, "str": str}.get(typ, typ)
        if not _type_ok(value, typ):
            raise ConfigFileError(
                f"bad config file {path}: {name} must be {typ.__name__}, "
                f"got {type(value).__name__} {value!r}")
    cost = data.get("round_turn_cost", 0.0)
    if not math.isfinite(cost) or cost < 0:
        raise ConfigFileError(
            f"bad config file {path}: round_turn_cost must be a finite number >= 0, "
            f"got {cost!r}")
    return InstrumentConfig(**data)


def get_config(name: str = "NQ") -> InstrumentConfig:
    """Return the InstrumentConfig registered under `name` (default 'NQ').

    A `name` ending in ``.json`` or naming an existing file is loaded with
    load_config_file instead.
    """
    if name not in _CONFIGS and (name.lower().endswith(".json") or os.path.isfile(name)):
        return load_config_file(name)
    try:
        return _CONFIGS[name]
    except KeyError as err:
        raise KeyError(
            f"unknown instrument config {name!r}; known: {sorted(_CONFIGS)} "
            "or a path to a JSON config file") from err
