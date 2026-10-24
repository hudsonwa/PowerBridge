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


"""Reporting methods of `engine.FillEngine` (a mixin): position and
performance kwargs, MultiCharts trade export and trace injection."""

from typing import TYPE_CHECKING, Any, Optional

from pl_transpiler.bar_loop import _PERF_KWARGS
from pl_transpiler.engine_dates import el_to_ole
from pl_transpiler.runtime.instrument_config import NQ

if TYPE_CHECKING:
    from pl_transpiler.runtime.instrument_config import InstrumentConfig


class _ReportMixin:
    if TYPE_CHECKING:
        # Host (FillEngine) state this mixin reads.
        config: Optional[InstrumentConfig]
        market_position: int
        contracts: int
        entry_price: float
        avgentryprice: float
        bars_since_entry: int
        exit_price: float
        bars_since_exit: int
        open_position_profit: float
        position_profit: float
        net_profit: float
        gross_profit: float
        gross_loss: float
        total_trades: int
        max_contracts_held: int
        max_id_drawdown: float
        entry_date: int
        entry_time: int
        entry_name: str
        fills: list[dict[str, Any]]
        fills_v1: list[dict[str, Any]]
        closed_trades: list[dict[str, Any]]
        _entry_qtys: list[int]
        _emit_gta6: bool
        _pos_mfe_pc: float
        _pos_mae_pc: float
        _pos_max_contracts: int
        _pos_entries: int
        _st_nwin: int
        _st_nlos: int
        _st_neven: int
        _st_lrgwin: float
        _st_lrglos: float
        _st_totbwin: int
        _st_totblos: int
        _st_totbeven: int
        _st_maxcwin: int
        _st_maxclos: int

    def fill_pos_kwargs(self, d: dict[str, Any]) -> None:
        """Write position kwargs into *d* in place (no new dict)."""
        d['market_position'] = self.market_position
        d['current_contracts'] = self.contracts
        d['entry_price'] = self.entry_price
        d['entry_date'] = self.entry_date
        d['entry_time'] = self.entry_time
        d['avgentryprice'] = self.avgentryprice
        d['bars_since_entry'] = self.bars_since_entry
        d['exit_price'] = self.exit_price
        d['bars_since_exit'] = self.bars_since_exit
        d['open_position_profit'] = self.open_position_profit
        d['position_profit'] = self.position_profit
        d['net_profit'] = self.net_profit
        d['gross_profit'] = self.gross_profit
        d['gross_loss'] = self.gross_loss
        d['totaltrades'] = self.total_trades
        d['maxcontractsheld'] = self.max_contracts_held
        d['maxiddrawdown'] = self.max_id_drawdown
        d['maxshares'] = self.max_shares
        d['openentriescount'] = self.open_entries_count
        for k, a in _PERF_KWARGS:
            d[k] = getattr(self, a)

    # Closed-trade performance words, read from the running aggregates kept at
    # trade close (_record_closed_trade); same values as _inject_gta6_stats.
    @property
    def pct_profit(self) -> float:
        tt = self._st_nwin + self._st_nlos + self._st_neven
        return (self._st_nwin / tt * 100.0) if tt else 0.0

    @property
    def avg_bars_win(self) -> float:
        return (self._st_totbwin / self._st_nwin) if self._st_nwin else 0.0

    @property
    def avg_bars_los(self) -> float:
        return (self._st_totblos / self._st_nlos) if self._st_nlos else 0.0

    @property
    def avg_bars_even(self) -> float:
        return (self._st_totbeven / self._st_neven) if self._st_neven else 0.0

    @property
    def max_shares(self) -> int:
        """MaxShares / MaxShares(0): most contracts held while the current
        position is open; 0 when flat."""
        return self._pos_max_contracts if self.market_position != 0 else 0

    @property
    def open_entries_count(self) -> int:
        """OpenEntriesCount: entries of the current position with contracts still
        open (a FIFO exit that uses up an entry removes it); 0 when flat."""
        return len(self._entry_qtys) if self.market_position != 0 else 0

    def get_kwargs(self) -> dict[str, Any]:
        """Return position kwargs to pass to strategy()."""
        d: dict[str, Any] = {}
        self.fill_pos_kwargs(d)
        return d

    def get_mc_trades(self, v1: bool = False) -> list[dict[str, Any]]:
        """Return list of MC-format trade dicts: order_no, type, signal, date, time, price, contracts.

        ``type`` is the order side: 'EntryLong' = buy side (Buy/BuyToCover),
        'ExitLong' = sell side (Sell/SellShort). See pl_oracle.fill_trade_types
        for the position-aware label. ``v1=True`` returns the pre-v2 log
        (no reversal open legs, no risk exits) for ``pl_oracle --digest-v1``."""
        trades: list[dict[str, Any]] = []
        for i, fill in enumerate(self.fills_v1 if v1 else self.fills):
            trades.append({
                'order_no': i + 1,
                'type': fill['type'],
                'signal': fill['signal'],
                'date': fill['date'],
                'time': fill['time'],
                'price': fill['price'],
                'contracts': fill['contracts'],
            })
        return trades

    def inject_trace(self, trace: dict[str, Any]) -> None:
        """Inject position state into trace for CSV output."""
        trace['_market_position'] = self.market_position
        trace['current_contracts'] = self.contracts
        trace['entry_price'] = self.entry_price
        trace['avgentryprice'] = self.avgentryprice
        trace['bars_since_entry'] = self.bars_since_entry
        trace['exit_price'] = self.exit_price
        trace['bars_since_exit'] = self.bars_since_exit
        trace['open_position_profit'] = self.open_position_profit
        trace['position_profit'] = self.position_profit
        trace['net_profit'] = self.net_profit
        trace['gross_profit'] = self.gross_profit
        trace['gross_loss'] = self.gross_loss
        trace['totaltrades'] = self.total_trades
        trace['maxcontractsheld'] = self.max_contracts_held
        trace['maxiddrawdown'] = self.max_id_drawdown
        # Skip the (now O(1)) GTA6 stat/introspection injection for captures whose
        # header contains none of those columns — pure overhead avoidance; it never
        # changes a value that IS in a header (see _emit_gta6 setup in run_gt).
        if self._emit_gta6:
            self._inject_gta6_stats(trace)
            self._inject_gta6_introspection(trace)

    def _inject_gta6_introspection(self, trace: dict[str, Any]) -> None:
        """GTA6 File-2 open-position + closed-trade introspection and entry/exit metadata.

        OpenEntry*(0) describes the CURRENT open position's (single) entry; PosTrade*(1,0)
        and Exit*(1) describe the most recently CLOSED position (PosBack=1, pdf:11918).
        Per-contract excursions feed OpenEntryMaxProfit/MinProfit (pdf:12085); _pos_mfe_pc/
        _pos_mae_pc are already in $ per contract. Instrument-dictionary constants
        (Symbol/SymbolName/Description/Currency/Expiration) are the static @NQ#C properties
        of the fundamental feed (ground_truth/captures/@NQ#C 1 Minute.txt = E-MINI NASDAQ 100),
        the same known-chart-config grounding used for session times / point values."""
        in_pos = self.market_position != 0
        contracts = self.contracts
        # --- OpenEntry*(0): the current open position's entry (index 0) ---
        trace['oecount'] = self._pos_entries if in_pos else 0
        trace['oeprice'] = self.entry_price if in_pos else 0.0
        trace['oeprofit'] = self.open_position_profit if in_pos else 0.0
        trace['oemaxprofit'] = self._pos_mfe_pc * contracts if in_pos else 0.0
        trace['oeminprofit'] = self._pos_mae_pc * contracts if in_pos else 0.0
        trace['oedate'] = self.entry_date if in_pos else 0
        trace['oetime'] = self.entry_time if in_pos else 0
        trace['oecontracts'] = contracts if in_pos else 0
        trace['oeprofitpc'] = (self.open_position_profit / contracts) if (in_pos and contracts) else 0.0
        trace['oemaxprofitpc'] = self._pos_mfe_pc if in_pos else 0.0
        trace['oeminprofitpc'] = self._pos_mae_pc if in_pos else 0.0
        # --- Entry*(0): the current open position's entry metadata ---
        trace['ennm'] = self.entry_name if in_pos else ''
        trace['endate'] = self.entry_date if in_pos else 0
        trace['entime'] = self.entry_time if in_pos else 0
        trace['endtm'] = el_to_ole(self.entry_date, self.entry_time) if in_pos else 0.0
        trace['curentr'] = self._pos_entries if in_pos else 0
        trace['curshr'] = contracts if in_pos else 0
        trace['execoff'] = 0
        # --- PosTrade*(1,0) + Exit*(1): the most recently CLOSED position ---
        if self.closed_trades:
            t = self.closed_trades[-1]
            trace['ptcount'] = t['entries']
            trace['ptprofit'] = t['pnl']
            trace['ptsize'] = t['size']
            trace['ptislong'] = 1 if t['islong'] else 0
            trace['ptisopen'] = 0
            trace['ptentrypx'] = t['entry_px']
            trace['ptexitpx'] = t['exit_px']
            trace['ptentrybar'] = t['entry_bar']
            trace['ptexitbar'] = t['exit_bar']
            trace['ptentrydtm'] = el_to_ole(t['entry_date'], t['entry_time'])
            trace['ptexitdtm'] = el_to_ole(t['exit_date'], t['exit_time'])
            trace['ptentrycat'] = t['entry_cat']
            trace['ptexitcat'] = t['exit_cat']
            trace['ptentrynm'] = t['entry_name']
            trace['ptexitnm'] = t['exit_name']
            trace['exnm'] = t['exit_name']
            trace['exdate'] = t['exit_date']
            trace['extime'] = t['exit_time']
            trace['exdtm'] = el_to_ole(t['exit_date'], t['exit_time'])
        else:
            for k in ('ptcount', 'ptprofit', 'ptsize', 'ptislong', 'ptisopen',
                      'ptentrypx', 'ptexitpx', 'ptentrybar', 'ptexitbar',
                      'ptentrydtm', 'ptexitdtm', 'ptentrycat', 'ptexitcat',
                      'exdate', 'extime', 'exdtm'):
                trace[k] = 0
            trace['ptentrynm'] = ''
            trace['ptexitnm'] = ''
            trace['exnm'] = ''
        # --- instrument-dictionary constants (static instrument properties) ---
        # Sourced from the per-run InstrumentConfig (NQ today), not hard-coded, so
        # the runtime is instrument-agnostic. Fall back to the NQ config when no
        # config was injected (preserves prior @NQ#C behavior).
        cfg = self.config if self.config is not None else NQ
        trace['symccy'] = cfg.symccy
        trace['symnm'] = cfg.symnm
        trace['symalt'] = cfg.symalt
        trace['descstr'] = cfg.descstr
        trace['expdate'] = cfg.expdate

    def _inject_gta6_stats(self, trace: dict[str, Any]) -> None:
        """GTA6 File-2 strategy-performance stats, computed from the closed-trade
        ledger + the open position. Each cited to PowerLanguage Keyword Reference:
          NumWin/Los/EvenTrades, Largest{Win,Los}Trade, PercentProfit (pp.~ stats),
          MaxConsec{Winners,Losers} (pdf:10550), AvgBars*/TotalBars* trade,
          MaxContractProfit (pdf:11405 open posn), MaxPositionProfit/Loss(1) (pdf:11547/
          11507, one posn back), MaxContracts/Shares (pdf:11433/11601), MaxEntries
          (pdf:11471), MaxSharesHeld (overall), MaxPositionsAgo (pdf:11587)."""
        # Read the incremental running aggregates (maintained at trade close in
        # _record_closed_trade) instead of rescanning self.closed_trades every bar.
        nwin = self._st_nwin
        nlos = self._st_nlos
        neven = self._st_neven
        tt = nwin + nlos + neven  # == len(closed_trades): every trade is win/loss/even
        trace['nwin'] = nwin
        trace['nlos'] = nlos
        trace['neven'] = neven
        trace['lrgwin'] = self._st_lrgwin
        trace['lrglos'] = self._st_lrglos
        trace['pctprofit'] = (nwin / tt * 100.0) if tt else 0.0
        # MaxConsecWinners/Losers: longest run of consecutive win/loss closed trades.
        trace['maxcwin'] = self._st_maxcwin
        trace['maxclos'] = self._st_maxclos
        totbwin = self._st_totbwin
        totblos = self._st_totblos
        totbeven = self._st_totbeven
        trace['totbwin'] = totbwin
        trace['totblos'] = totblos
        trace['totbeven'] = totbeven
        trace['avgbwin'] = (totbwin / nwin) if nwin else 0.0
        trace['avgblos'] = (totblos / nlos) if nlos else 0.0
        trace['avgbeven'] = (totbeven / neven) if neven else 0.0
        # MaxContractProfit (no PosBack) = OPEN position's max favorable excursion
        # per contract; 0 when flat.
        trace['maxctrprofit'] = self._pos_mfe_pc if self.market_position != 0 else 0.0
        # MaxPositionProfit(1)/MaxPositionLoss(1)/MaxContracts(1)/MaxEntries(1) refer to
        # the most recently CLOSED position (one position back) — read O(1) via [-1].
        if self.closed_trades:
            last = self.closed_trades[-1]
            trace['maxposprofit'] = last['mfe_pc'] * last['contracts']
            trace['maxposloss'] = last['mae_pc'] * last['contracts']
            trace['maxctr'] = last['contracts']
            trace['maxentr'] = last['entries']
        else:
            trace['maxposprofit'] = 0.0
            trace['maxposloss'] = 0.0
            trace['maxctr'] = 0
            trace['maxentr'] = 0
        # MaxShares (= MaxContracts, open position) and MaxSharesHeld (overall max held).
        trace['maxshr'] = self._pos_max_contracts if self.market_position != 0 else 0
        trace['maxshrheld'] = self.max_contracts_held
        # MaxPositionsAgo = number of positions taken so far (closed + the current) =
        # total closed trades + 1 (pdf:11587).
        trace['maxposago'] = self.total_trades + 1
