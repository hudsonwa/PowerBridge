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
instrument_config.py — per-run chart/instrument configuration (GOAL unit #4).

The pure-Python runner/runtime must be instrument-agnostic: the ~dozen
chart/instrument constants that make a run NQ-specific (point value, tick,
min-move, price scale, session start, bar interval, and the @NQ#C
instrument-dictionary properties) belong in a PER-RUN config object, NOT baked
into the shared runtime (transpiler/runtime/pl_runtime.py) or hard-coded in
tests/run_gt.py.

This module holds:
  * InstrumentConfig — an immutable dataclass of those constants.
  * NQ — the config reproducing today's exact validated @NQ#C values.
  * get_config(name) — select a config by name for a GT run.

Integrity note: these are KNOWN chart/instrument config (sourced from the chart
spec, not from the capture under test), so injecting them per-run preserves the
audited non-circular sourcing (see the AUDIT 2026-06-19 note in run_gt.py).
"""
from dataclasses import dataclass


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


# The NQ config reproduces today's exact validated values (previously hard-coded
# in tests/run_gt.py extra_kwargs + FillEngine). Selected for all current GT
# labels, which are all @NQ#C captures.
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
)


_CONFIGS = {
    "NQ": NQ,
}


def get_config(name="NQ"):
    """Return the InstrumentConfig registered under `name` (default 'NQ')."""
    try:
        return _CONFIGS[name]
    except KeyError:
        raise KeyError(
            f"unknown instrument config {name!r}; known: {sorted(_CONFIGS)}")
