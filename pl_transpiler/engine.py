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

"""engine.py -- shared bar-by-bar execution engine.

The stateful order/fill engine (:class:`FillEngine`), plus re-exports of the
per-bar execution core (:func:`run_bar_loop`, in ``bar_loop``) and the EL
date/time parsers (in ``engine_dates``), extracted from
tests/run_gt.py so the SHIPPED package is self-contained. The installed ``pl_run``
console script (pl_transpiler.tools.pl_run) drives this engine directly from a
bare wheel, with no dependency on the private tests/ harness.

tests/run_gt.py re-imports these names (run_gt.FillEngine,
run_gt.run_bar_loop, run_gt._parse_el_date, ...), so this module is the single
definition of them.
"""
from pl_transpiler.runtime.pl_runtime import f32
from pl_transpiler.runtime.instrument_config import NQ

from pl_transpiler.engine_dates import (  # noqa: F401  (re-export)
    _parse_el_date,
    _parse_el_time,
    _el_to_ole,
    _el_date_to_day_of_week,
)


class FillEngine:
    """Fill engine: tracks position state, processes orders, tracks PnL.

    Implements the EL fill model:
    - "next bar at market" fills at the next bar's Open price
    - "next bar at <price> stop/limit" fills on the next bar when its range
      reaches the price (see _execute_pending_fills for prices and ordering)
    - "this bar on close" fills at the signal bar's Close
    - Money management stops: SetStopLoss, SetProfitTarget, SetBreakEven,
      SetPercentTrailing, SetDollarTrailing, SetExitOnClose, SetStopContract
    - Default point value 20 (NQ, per contract per full point); a config
      overrides it
    """

    def __init__(self, point_value=20, tick=0.25, config=None, max_entries_per_direction=0):
        # Per-run instrument config (InstrumentConfig). When provided, it is the
        # single source of the chart/instrument constants — point_value (=$/point)
        # and tick are derived from it so the fill PnL responds to the injected
        # config rather than a hard-coded 20. point_value/tick remain as explicit
        # fallbacks for callers that don't pass a config (e.g. an ES point value of 50).
        self.config = config
        if config is not None:
            point_value = config.bigpointvalue
            tick = config.tick
        # Same-direction entry cap. 0 (default) = unlimited: every Buy /
        # SellShort adds, as with MultiCharts' "allow multiple entries" setting.
        # N >= 1 refuses an entry while N entries are open in its direction
        # (N=1 is MultiCharts' documented default). Reversals are never refused.
        self.max_entries_per_direction = int(max_entries_per_direction or 0)
        self.market_position = 0    # 0=flat, 1=long, -1=short
        self.contracts = 0
        self.entry_price = 0.0
        self.avgentryprice = 0.0
        self.bars_since_entry = 0
        self.exit_price = 0.0
        self.bars_since_exit = 0
        self.open_position_profit = 0.0
        self.position_profit = 0.0
        self.net_profit = 0.0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        self.total_trades = 0
        self.max_contracts_held = 0
        self.max_id_drawdown = 0.0
        self.entry_bar = 0
        self.exit_bar = 0
        self.point_value = point_value
        # Running peak equity (net_profit + open_position_profit) for drawdown tracking.
        self.peak_equity = 0.0
        # Instrument minimum price increment. MultiCharts snaps money-management
        # stop/target/trailing/breakeven FILL prices to the tick grid, so a
        # trail level computed off a dollar peak (e.g. 18692.775) fills at the
        # nearest tradable price (18692.75). Only used by the MM-stop path
        # (SetStopLoss / SetProfitTarget / SetPercentTrailing / SetBreakEven),
        # which the reference strategy never engages — it computes its stops in
        # EL and exits via explicit Sell orders, so this does not affect parity.
        self.tick = tick
        self._pending_orders = []   # List of (action, otype, qty, timing, label, price)
        self._onclose_pending = []  # 'this bar on close' fills, applied at next before_bar
        self._risk = {}             # Current money-management settings
        self._breakeven_triggered = False  # SetBreakEven has been triggered for current position
        self._peak_profit = 0.0     # Running max profit seen since entry (used by trailing/breakeven)
        # MC-format trade tracking
        self.fills = []             # List of dicts: {type, signal, date, time, price, contracts}
        # The pre-v2 fill log, kept only so `pl_oracle --digest-v1` reproduces
        # digests pinned before reversals and risk exits were recorded.
        self.fills_v1 = []
        # FIFO entry-price queue: one entry per contract, in fill order.
        # Used to recalculate AvgEntryPrice after partial exits (TWAP scale-out).
        self._entry_prices = []     # List of float, one per contract
        # Open quantity of each entry of the current position, in fill order. FIFO
        # exits consume it; OpenEntriesCount counts the entries still above zero.
        self._entry_qtys = []
        # GTA6 File-2 strategy-performance stats: a per-closed-position ledger plus
        # the OPEN position's running excursions. Each closed-trade snapshot records
        # the realized pnl, bars-in-trade (exit_bar - entry_bar + 1), the max favorable
        # / adverse excursion PER CONTRACT (MaxPositionProfit/Loss, MaxContractProfit),
        # the max contracts held (MaxContracts/MaxShares), and the entry count
        # (MaxEntries). All cited to PowerLanguage Keyword Reference pp.797-808.
        self.closed_trades = []     # list of dicts (one per closed position)
        # Incremental GTA6-stat aggregates, updated at trade CLOSE in
        # _record_closed_trade so _inject_gta6_stats reads O(1) running fields
        # instead of rescanning the entire growing ledger every bar (a rescan is
        # O(trades) per bar -> O(trades^2) over a full-range run).
        # Integer counts/sums/run-lengths and selected float extremes are exactly
        # reproducible, so these equal a full rescan byte for byte.
        self._st_nwin = 0           # NumWinTrades
        self._st_nlos = 0           # NumLosTrades
        self._st_neven = 0          # NumEvenTrades
        self._st_lrgwin = 0.0       # LargestWinTrade (max pnl over wins, floor 0.0)
        self._st_lrglos = 0.0       # LargestLosTrade (min pnl over losses, floor 0.0)
        self._st_totbwin = 0        # TotalBarsWinTrades
        self._st_totblos = 0        # TotalBarsLosTrades
        self._st_totbeven = 0       # TotalBarsEvenTrades
        self._st_cwin = 0           # current consecutive-winners run length
        self._st_clos = 0           # current consecutive-losers run length
        self._st_maxcwin = 0        # MaxConsecWinners
        self._st_maxclos = 0        # MaxConsecLosers
        # When False, skip GTA6 stat/introspection injection entirely for captures
        # whose header contains none of those columns (e.g. GT2). Set per-run from
        # the capture header; defaults True (inject).
        self._emit_gta6 = True
        self._pos_mfe_pc = 0.0      # open position: max favorable excursion per contract ($)
        self._pos_mae_pc = 0.0      # open position: max adverse excursion per contract ($, <=0)
        self._pos_max_contracts = 0 # open position: max contracts held while open
        self._pos_entries = 0       # open position: number of separate entries
        # GTA6 File-2 trade-introspection (OpenEntry* / PosTrade* / Entry-Exit metadata):
        # the OPEN position's entry context (date/time/name/order-category), captured at
        # the entry fill, and a per-closed-trade ledger of entry/exit context so the
        # PosTrade*(1,0) and Exit*(1) words can be reconstructed (pdf:11911-12217).
        self.entry_date = 0         # open position: entry bar EL date (YYMMDD)
        self.entry_time = 0         # open position: entry bar EL time (HHMM)
        self.entry_name = ''        # open position: entry signal name
        self.entry_cat = 0          # open position: entry order category (3 = Market, pdf:11953)
        # Fast-path instrumentation: number of flat, no-work bars whose
        # before_bar lifecycle was skipped. A public counter — the transition
        # tests and the oracle status line read it to prove the fast path fired.
        self.fast_path_hits = 0

    def _has_active_risk(self):
        """True iff any money-management / session risk setting is armed.

        Mirrors the native kernel's RiskSettings exactly (stop_loss,
        profit_target, breakeven, dollar_trailing, percent_trailing with a
        positive percent, and exit_on_close). A positive-value-zero-disabled setting (e.g.
        SetStopLoss(0)) is inert, matching ``_check_money_management_stops``.
        """
        r = self._risk
        for key in ('stop_loss', 'profit_target', 'breakeven', 'dollar_trailing'):
            v = r.get(key, 0) or 0
            if v > 0:
                return True
        pt = r.get('percent_trailing')
        if (isinstance(pt, (list, tuple)) and len(pt) == 2
                and (pt[1] or 0) > 0):
            return True
        return bool(r.get('exit_on_close', False))

    def no_work_state(self):
        """Exact predicate for the bar the engine can skip entirely.

        True iff the position is flat, no next-bar or on-close order is queued,
        and no risk is armed. In that state every ``before_bar`` computation is
        provably a no-op: pending/on-close fills are empty, money-management
        stops early-return when flat, open PnL stays 0.0, excursion is skipped,
        and the drawdown fold is inert because net_profit cannot have changed
        (net_profit mutates only via a fill or a session-rollover close, both of
        which imply a non-empty order queue or an armed exit_on_close).
        """
        return (self.market_position == 0
                and not self._pending_orders
                and not self._onclose_pending
                and not self._has_active_risk())

    def before_bar(self, open_price, close_price, high, low, barnumber, date_str=0, time_str=0):
        """Process pending fills and compute PnL BEFORE strategy runs."""
        if self.no_work_state():
            self.fast_path_hits += 1
            return
        self._execute_onclose_pending()
        self._execute_pending_fills(open_price, barnumber, date_str, time_str,
                                    high=high, low=low, close_price=close_price)
        self._check_money_management_stops(high, low, close_price, barnumber, open_price,
                                           date_str, time_str)
        self._compute_pnl(close_price)
        # Track the open position's max favorable/adverse excursion using this bar's
        # high/low (feeds MaxContractProfit / MaxPositionProfit / MaxPositionLoss).
        self.update_excursion(high, low)
        # Update bars_since_entry from entry_bar
        if self.market_position != 0 and self.entry_bar > 0:
            self.bars_since_entry = barnumber - self.entry_bar
        # Update drawdown tracking (MaxIDDrawDown): the maximum peak-to-trough
        # decline in total equity (net_profit + open_position_profit). MultiCharts
        # walks equity along the bar's intrabar price path (Bar Magnifier OFF),
        # assuming the extreme NEAREST THE OPEN is reached first:
        #   open in upper half (closer to High): O -> H -> L -> C
        #   open in lower half (closer to Low):  O -> L -> H -> C
        # so a same-bar far extreme cannot open a drawdown against the near extreme
        # the path reaches first. Verified exact on the GTA5 o1/o3 drawdown edge bars
        # where this disagrees with a close-vs-open rule (o3 bn42 open-near-high ->
        # H-first=-220; o1 bn252 open-near-low -> L-first=-190; o1 bn244 doji
        # open-near-high -> H-first=-80). Market/on-close fills land at a path endpoint
        # so OrderMode-0/3/4 entry geometry is unaffected. Strict '<' so an EXACTLY
        # open-centered bar (high-open == open-low) resolves LOW-first, matching MC's
        # tie convention (verified o2 bn46252 tie -> deeper drawdown -24220 not -24130).
        if (high - open_price) < (open_price - low):
            path = (open_price, high, low, close_price)
        else:
            path = (open_price, low, high, close_price)
        if self.market_position != 0 and self.contracts > 0:
            sign = 1 if self.market_position > 0 else -1
            # On the bar a position is ENTERED via an intrabar limit/stop fill, only
            # the POST-fill portion of the bar counts toward the position's drawdown
            # (the pre-fill excursion is not yet owned by this position). Market and
            # on-close entries fill at a path endpoint, so this is a no-op for them
            # (OrderMode-0/3/4 unaffected); it corrects the limit/stop entry bar.
            walk = path
            if self.entry_bar == barnumber:
                walk = self._post_entry_walk(path, self.entry_price)
            eqs = [self.net_profit + sign * (px - self.entry_price) * self.point_value * self.contracts
                   for px in walk]
        else:
            eqs = [self.net_profit]
        for eq in eqs:
            self.peak_equity = max(self.peak_equity, eq)
            dd = self.peak_equity - eq
            if dd > abs(self.max_id_drawdown):
                self.max_id_drawdown = f32(-dd)

    @staticmethod
    def _post_entry_walk(path, entry_price):
        """Sub-path from an intrabar fill point to the bar close: the first path
        segment that straddles entry_price, then [entry_price, *remaining points]."""
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            if min(a, b) <= entry_price <= max(a, b):
                return (entry_price,) + tuple(path[i + 1:])
        return tuple(path)

    def after_bar(self, orders, risk, close_price, barnumber=0, date_str=0, time_str=0):
        """Queue new orders AFTER strategy runs and compute end-of-bar PnL.

        "This bar on close" orders (OrderMode 3: `Buy/SellShort This Bar on Close`)
        fill at THIS bar's close, applied AFTER the strategy has already printed its
        per-bar row — so the signal bar still shows the pre-fill position and the
        NEXT bar reflects the new position with entry_price == this bar's close
        (verified against GTA5_o3 capture: bn41 tag=LE_CLS mp=0, bn42 mp=1 entry=close41).
        All other orders ('next' timing) stay pending for the next bar's open.
        """
        valid = [o for o in orders if self._is_valid_order(o)]
        pending = []
        for o in valid:
            otype = o[1].lower() if len(o) > 1 else ''
            timing = o[3].lower() if len(o) > 3 else 'next'
            if timing in ('this', 'thisbar') and otype == 'close':
                # Fill price = THIS bar's close, but apply at the NEXT bar's
                # before_bar so this (signal) bar's injected trace stays pre-fill
                # (MC shows the on-close position effect starting the next bar; the
                # entry is attributed to THIS bar/close — entry_bar = barnumber).
                self._onclose_pending.append((o, f32(close_price), barnumber, date_str, time_str))
            else:
                pending.append(o)
        self._pending_orders = pending
        self._risk.update(self._resolve_point_risk(risk))
        self._compute_pnl(close_price)

    # SetStopLoss_pt & co. (MultiCharts point-based variants): risk key -> the
    # currency key it arms once the amount is scaled by BigPointValue.
    _POINT_RISK_KEYS = (
        ('stop_loss_pt', 'stop_loss'),
        ('profit_target_pt', 'profit_target'),
        ('breakeven_pt', 'breakeven'),
        ('trailing_stop_pt', 'dollar_trailing'),
    )

    def _resolve_point_risk(self, risk):
        """Convert this bar's point-based risk settings to currency amounts.

        A ``*_pt`` amount is in price points, so it arms the same level as the
        dollar form given ``points * BigPointValue``; SetPercentTrailing_pt's
        profit threshold is scaled the same way and its percent is unchanged.
        Risk dicts with no ``*_pt`` key are returned untouched.
        """
        if not any(k.endswith('_pt') for k in risk):
            return risk
        risk = dict(risk)
        pv = self.point_value
        for pt_key, key in self._POINT_RISK_KEYS:
            if pt_key in risk:
                risk[key] = risk.pop(pt_key) * pv
        if 'percent_trailing_pt' in risk:
            pts, pct = risk.pop('percent_trailing_pt')
            risk['percent_trailing'] = (pts * pv, pct)
        return risk

    def _execute_onclose_pending(self):
        """Apply any 'this bar on close' fills stashed at the previous bar's close.
        Called at the very start of before_bar, so the position flips between bars."""
        if not self._onclose_pending:
            return
        for order, price, bn, ds, ts in self._onclose_pending:
            self._fill_close_order(order, price, bn, ds, ts)
        self._onclose_pending = []

    def _entry_refused(self, action):
        """True when ``max_entries_per_direction`` blocks this entry: it adds
        in the open position's direction and the cap is already reached."""
        cap = self.max_entries_per_direction
        if cap <= 0:
            return False
        same_dir = ((action == 'buy' and self.market_position > 0)
                    or (action == 'sellshort' and self.market_position < 0))
        return same_dir and self.open_entries_count >= cap

    def _fill_close_order(self, order, close_price, barnumber, date_str=0, time_str=0):
        """Fill a 'this bar on close' order at the stored (signal-bar) close price."""
        action = order[0].lower()
        if self._entry_refused(action):
            return
        qty = int(order[2]) if len(order) > 2 and order[2] else 1
        label = order[4] if len(order) > 4 else ''
        prev_contracts = self.contracts
        prev_signed = self._signed_position()
        self._fill(action, qty, close_price, barnumber,
                   date=date_str, time=time_str, label=label, cat=3)
        self._record_fills(action in ('buy', 'buytocover'), label, date_str, time_str,
                           close_price, prev_signed,
                           v1_qty=abs(self.contracts - prev_contracts) or qty)

    def _is_valid_order(self, order):
        """Check that order tuple has expected structure."""
        if not isinstance(order, tuple) or len(order) < 5:
            return False
        return True

    def _execute_pending_fills(self, open_price, barnumber, date_str=0, time_str=0,
                               high=None, low=None, close_price=None):
        """Fill next-bar pending orders against THIS bar's prices.

        Order types (price at order[5]; the EL verb decides the trigger side):
          * market : fills at the Open.
          * limit  : buy-side fills if Low <= px (at min(Open,px)); sell-side fills
                     if High >= px (at max(Open,px)).  [LimitBuyPx=Low / LimitShortPx=High]
          * stop   : buy-side fills if High >= px (at max(Open,px)); sell-side fills
                     if Low <= px (at min(Open,px)).  [StopBuyPx=High / StopShortPx=Low]
        A protective stop + profit target queued together (OrderMode-4 bracket) are
        resolved by which leg MC touches first: an order the OPEN already satisfies
        fills at the open instant; otherwise the open-relative intrabar path (extreme
        nearest the open first) decides. The other leg no-ops once the position is
        flat. Verified exact on GTA5 o1/o2 entries and o4 brackets (bn43/3358/6435).
        """
        if not self._pending_orders:
            return
        market_orders, cond_orders = [], []
        for order in self._pending_orders:
            otype = order[1].lower()
            timing = order[3].lower() if len(order) > 3 else 'next'
            if timing != 'next':
                continue
            if otype == 'market':
                market_orders.append(order)
            elif otype in ('limit', 'stop') and high is not None and low is not None:
                price = order[5] if len(order) > 5 and isinstance(order[5], (int, float)) else None
                if price is None:
                    continue
                is_buy = order[0].lower() in ('buy', 'buytocover')
                side = ('low' if is_buy else 'high') if otype == 'limit' \
                    else ('high' if is_buy else 'low')
                if side == 'low' and low <= price:
                    # at_open: the OPEN already satisfies the order, so it fills at
                    # the open (the bar's first price), ahead of intrabar triggers.
                    cond_orders.append(('low', open_price <= price, order, f32(min(open_price, price))))
                elif side == 'high' and high >= price:
                    cond_orders.append(('high', open_price >= price, order, f32(max(open_price, price))))
        # Market fills at the open, before any intrabar extreme.
        for order in market_orders:
            self._fill_at(order, open_price, barnumber, date_str, time_str)
        # Conditional fills, ordered the way MC's intrabar path would touch them:
        #   1. orders the OPEN already satisfies fill first (at the open instant);
        #   2. the rest fill in the open-relative path order (extreme nearest the
        #      open first — the SAME assumption the drawdown calc uses).
        # This resolves same-bar stop+limit brackets to the leg MC fills. Verified on
        # o4: bn43 stop@open -> stop first (-50); bn3358 neither@open, open-near-high
        # -> limit first (+80); bn6435 limit@open (open==limit) -> limit first (+80).
        if cond_orders:
            if high is not None and low is not None:
                # Strict '<' so an EXACTLY open-centered bar (high-open == open-low)
                # resolves LOW-first — MC fills the stop on the tie (verified o4 bn18178,
                # where O is dead-center and the protective stop, not the limit, fills).
                first = 'high' if (high - open_price) < (open_price - low) else 'low'
            else:
                first = 'low'
            cond_orders.sort(key=lambda x: (0 if x[1] else 1, 0 if x[0] == first else 1))
            for _side, _atopen, order, fpx in cond_orders:
                self._fill_at(order, fpx, barnumber, date_str, time_str)
        self._pending_orders = []

    def _fill_at(self, order, fill_price, barnumber, date_str=0, time_str=0):
        """Fill one order at the resolved price; record the fill. qty==0 (the
        OrderMode-4 exit orders emit 0) means exit the entire open position."""
        action = order[0].lower()
        if self._entry_refused(action):
            return
        raw_qty = order[2] if len(order) > 2 else 1
        if raw_qty == 0:
            # OrderMode-4 EXIT orders emit 0 to mean "close the whole position".
            # That rule belongs to CLOSING verbs only. Applying it to an entry
            # sized the order off the CURRENT position, so an unguarded per-bar
            # `Buy` doubled the position every bar — 2**n fills, OOM at ~200
            # bars. A bare entry (and the opening leg of a reversal) uses the
            # default order size of 1 contract. Keeping the exit rule on
            # 'sell'/'buytocover' preserves the reference strategy exactly.
            if action in ('sell', 'buytocover'):
                qty = self.contracts if self.contracts > 0 else 1
            else:
                qty = 1
        else:
            qty = int(raw_qty) if raw_qty else 1
        label = order[4] if len(order) > 4 else ''
        prev_contracts = self.contracts
        prev_signed = self._signed_position()
        self._fill(action, qty, fill_price, barnumber,
                   date=date_str, time=time_str, label=label, cat=3)
        self._record_fills(action in ('buy', 'buytocover'), label, date_str, time_str,
                           fill_price, prev_signed,
                           v1_qty=abs(self.contracts - prev_contracts))

    def _signed_position(self):
        """Open contracts, signed: + long, - short, 0 flat."""
        if self.market_position > 0:
            return self.contracts
        if self.market_position < 0:
            return -self.contracts
        return 0

    def _record_fills(self, buy_side, signal, date, time, price, prev_signed, v1_qty=0):
        """Append one record per contract the SIGNED position moved since
        prev_signed. A reversal (long 1 -> short 1) moves 2: the close leg and
        then the open leg, both on the order's side. 'type' is the buy/sell
        side (hashed by pl_oracle), not a position label;
        pl_oracle.fill_trade_types derives EntryShort/ExitShort."""
        mc_type = 'EntryLong' if buy_side else 'ExitLong'
        rec = {'type': mc_type, 'signal': signal, 'date': date,
               'time': time, 'price': price, 'contracts': 1}
        for _ in range(abs(self._signed_position() - prev_signed)):
            self.fills.append(dict(rec))
        for _ in range(v1_qty):
            self.fills_v1.append(dict(rec))

    def _fill(self, action, qty, fill_price, barnumber, date=0, time=0, label='', cat=3):
        """Execute a fill: update position, handle trade close/open.

        Dispatch is strictly on the EL order verb, because the verb — not the
        current position alone — decides whether a fill REVERSES or only CLOSES.
        This lets the single engine serve BOTH order styles that share it:

          * Reversal systems (GT2 / GT_master, OrderMode-0 market):
              - 'buy'       while short -> close short AND open long  (reverse)
              - 'sellshort' while long  -> close long  AND open short (reverse)
          * Scale-out / long-only systems (TWAP, embedded stops, trailing):
              - 'buy'       opens / adds to a long
              - 'sell'      closes long contracts FIFO, never flips to short
              - 'buytocover'closes short contracts, never flips to long

        EL verb semantics:
          - 'buy'        = enter/add long; if currently short, REVERSE to long.
          - 'sellshort'  = enter/add short; if currently long, REVERSE to short.
          - 'sell'       = close long only (FIFO). Never opens a short.
          - 'buytocover' = close short only. Never opens a long.

        The reference strategy only ever issues 'buy' (flat/long) and 'sell', so
        its behavior is identical to before this dispatch split — the 8/8 parity
        is preserved.
        """
        if action == 'buy':
            if self.market_position < 0:
                # Reversal: close the short, then open a long of `qty` at the
                # same fill price (EL flattens-and-flips in one market fill).
                self._close_short(fill_price, barnumber, date, time, label, cat)
                self._open_or_add_long(qty, fill_price, barnumber, date, time, label, cat)
            else:
                self._open_or_add_long(qty, fill_price, barnumber, date, time, label, cat)

        elif action == 'sellshort':
            if self.market_position > 0:
                # Reversal: close the long, then open a short of `qty`.
                self._close_long_fifo(self.contracts, fill_price, barnumber, date, time, label, cat)
                self._open_or_add_short(qty, fill_price, barnumber, date, time, label, cat)
            else:
                self._open_or_add_short(qty, fill_price, barnumber, date, time, label, cat)

        elif action == 'sell':
            # Close long contracts FIFO only — never reverse to short.
            if self.market_position > 0:
                self._close_long_fifo(qty, fill_price, barnumber, date, time, label, cat)
            # Flat/short: a 'sell' from flat does nothing (can occur when
            # multiple exit orders are queued on the same bar).

        elif action == 'buytocover':
            # Close short only — never reverse to long.
            if self.market_position < 0:
                self._close_short(fill_price, barnumber, date, time, label, cat)

    def _open_or_add_long(self, qty, fill_price, barnumber, date=0, time=0, name='', cat=3):
        """Open a new long or add to an existing long (weighted avg entry)."""
        fill_price = f32(fill_price)
        old_value = f32(self.entry_price * self.contracts)
        self.market_position = 1
        self.contracts = self.contracts + qty
        self._entry_prices.extend([fill_price] * qty)
        if self.contracts > 0:
            self.entry_price = f32((old_value + fill_price * qty) / self.contracts)
        else:
            self.entry_price = f32(fill_price)
        self.avgentryprice = self.entry_price
        if self.contracts == qty:  # first entry of the position
            self.entry_bar = barnumber
            self.position_profit = 0.0  # Reset for new position
            self._reset_position_stats()
            self.entry_date = date
            self.entry_time = time
            self.entry_name = name
            self.entry_cat = cat
            self._entry_qtys = [qty]
        else:
            self._pos_entries += 1
            self._entry_qtys.append(qty)
        self._pos_max_contracts = max(self._pos_max_contracts, self.contracts)
        self.bars_since_entry = 0
        self._breakeven_triggered = False
        self._peak_profit = 0.0
        self.max_contracts_held = max(self.max_contracts_held, self.contracts)

    def _open_or_add_short(self, qty, fill_price, barnumber, date=0, time=0, name='', cat=3):
        """Open a new short or add to an existing short (weighted avg entry)."""
        fill_price = f32(fill_price)
        old_value = f32(self.entry_price * abs(self.contracts))
        self.market_position = -1
        self.contracts = abs(self.contracts) + qty
        self._entry_prices.extend([fill_price] * qty)
        self.entry_price = f32((old_value + fill_price * qty) / self.contracts)
        self.avgentryprice = self.entry_price
        if self.contracts == qty:  # first entry of the position
            self.entry_bar = barnumber
            self._reset_position_stats()
            self.entry_date = date
            self.entry_time = time
            self.entry_name = name
            self.entry_cat = cat
            self._entry_qtys = [qty]
        else:
            self._pos_entries += 1
            self._entry_qtys.append(qty)
        self._pos_max_contracts = max(self._pos_max_contracts, self.contracts)
        self.bars_since_entry = 0
        self.position_profit = 0.0  # Reset for new position
        self._breakeven_triggered = False
        self._peak_profit = 0.0
        self.max_contracts_held = max(self.max_contracts_held, self.contracts)

    def _reset_position_stats(self):
        """Reset the OPEN-position excursion/size trackers at the start of a new position."""
        self._pos_mfe_pc = 0.0
        self._pos_mae_pc = 0.0
        self._pos_max_contracts = 0
        self._pos_entries = 1

    def update_excursion(self, high, low):
        """Update the open position's max favorable/adverse excursion PER CONTRACT
        using the current bar's high/low (MaxPositionProfit/Loss, MaxContractProfit
        are floored at 0 — pdf:11560,11520)."""
        if self.market_position == 0 or self.contracts <= 0:
            return
        pv = self.point_value
        if self.market_position > 0:
            fav = (high - self.entry_price) * pv
            adv = (low - self.entry_price) * pv
        else:
            fav = (self.entry_price - low) * pv
            adv = (self.entry_price - high) * pv
        if fav > self._pos_mfe_pc:
            self._pos_mfe_pc = fav
        if adv < self._pos_mae_pc:
            self._pos_mae_pc = adv

    def _record_closed_trade(self, exit_bar, exit_price=0.0, size=0, islong=True,
                             exit_date=0, exit_time=0, exit_name='', exit_cat=0):
        """Snapshot the just-closed position into the closed-trade ledger.

        Captures the per-position entry/exit context (price, bar, EL date/time,
        signal name, order category) so the PosTrade*(1,0) and Exit*(1) reserved
        words can be reconstructed for File-2 (pdf:11911-12217). Read at close time,
        BEFORE _reset_flat, so self.entry_* still hold the open position's context."""
        bars = (exit_bar - self.entry_bar + 1) if self.entry_bar > 0 else 0
        # MaxPositionLoss/Profit is "the largest loss/profit reached while the position
        # was HELD" (pdf:11508). The exit fill is part of the held period, but the
        # exit-bar excursion is folded in by update_excursion only AFTER the position
        # has been closed — so the realized exit price must be folded into the
        # per-contract MAE/MFE here, before the snapshot, or the exit move is lost.
        mfe_pc = self._pos_mfe_pc
        mae_pc = self._pos_mae_pc
        if exit_price and self.entry_price:
            pv = self.point_value
            exc = (f32(exit_price) - self.entry_price) * pv if islong \
                else (self.entry_price - f32(exit_price)) * pv
            if exc > mfe_pc:
                mfe_pc = exc
            if exc < mae_pc:
                mae_pc = exc
        self.closed_trades.append({
            'pnl': self.position_profit,
            'bars': bars,
            'mfe_pc': mfe_pc,
            'mae_pc': mae_pc,
            'contracts': self._pos_max_contracts,
            'entries': self._pos_entries,
            'size': size,
            'islong': islong,
            'entry_px': self.entry_price,
            'exit_px': f32(exit_price),
            'entry_bar': self.entry_bar,
            'exit_bar': exit_bar,
            'entry_date': self.entry_date,
            'entry_time': self.entry_time,
            'exit_date': exit_date,
            'exit_time': exit_time,
            'entry_name': self.entry_name,
            'exit_name': exit_name,
            'entry_cat': self.entry_cat,
            'exit_cat': exit_cat,
        })
        # Fold this just-closed trade into the incremental GTA6 aggregates (see
        # __init__). pnl/bars are the SAME values stored in the snapshot above, so
        # the running counts/sums/extremes/run-lengths reproduce a full
        # rescan exactly (win=pnl>0, loss=pnl<0, even=pnl==0; floors at 0.0).
        pnl = self.position_profit
        if pnl > 0:
            self._st_nwin += 1
            if pnl > self._st_lrgwin:
                self._st_lrgwin = pnl
            self._st_totbwin += bars
            self._st_cwin += 1
            self._st_clos = 0
            if self._st_cwin > self._st_maxcwin:
                self._st_maxcwin = self._st_cwin
        elif pnl < 0:
            self._st_nlos += 1
            if pnl < self._st_lrglos:
                self._st_lrglos = pnl
            self._st_totblos += bars
            self._st_clos += 1
            self._st_cwin = 0
            if self._st_clos > self._st_maxclos:
                self._st_maxclos = self._st_clos
        else:
            self._st_neven += 1
            self._st_totbeven += bars
            self._st_cwin = 0
            self._st_clos = 0

    def _close_long_fifo(self, qty, fill_price, exit_bar=0, exit_date=0, exit_time=0,
                         exit_name='', exit_cat=0):
        """Close `qty` long contracts FIFO; recompute avg entry on the remainder."""
        closed_qty = min(qty, self.contracts)
        closed_pnl = (fill_price - self.entry_price) * self.point_value * closed_qty
        self.net_profit += closed_pnl
        self.total_trades += 1
        self.position_profit += closed_pnl
        self._update_gross_pnl(closed_pnl)
        self.contracts = self.contracts - closed_qty
        for _ in range(closed_qty):
            if self._entry_prices:
                self._entry_prices.pop(0)
        self._consume_entry_qtys(closed_qty)
        if self.contracts > 0 and self._entry_prices:
            self.entry_price = f32(sum(self._entry_prices) / len(self._entry_prices))
            self.avgentryprice = self.entry_price
        elif self.contracts <= 0:
            self._record_closed_trade(exit_bar, fill_price, closed_qty, True,
                                      exit_date, exit_time, exit_name, exit_cat)
            self._reset_flat()

    def _consume_entry_qtys(self, qty):
        """Take `qty` contracts off the oldest entries first (FIFO)."""
        qtys = self._entry_qtys
        while qty > 0 and qtys:
            take = min(qty, qtys[0])
            qtys[0] -= take
            qty -= take
            if qtys[0] <= 0:
                qtys.pop(0)

    def _update_gross_pnl(self, pnl):
        """Track gross profit and loss (sum of winning/losing trades)."""
        if pnl > 0:
            self.gross_profit += pnl
        elif pnl < 0:
            self.gross_loss += pnl

    def _close_short(self, fill_price, exit_bar=0, exit_date=0, exit_time=0,
                     exit_name='', exit_cat=0):
        """Close the entire short position at `fill_price`."""
        fill_price = f32(fill_price)
        prev = self.contracts
        closed_pnl = (self.entry_price - fill_price) * self.point_value * prev
        self.net_profit += closed_pnl
        self.total_trades += 1
        self.position_profit += closed_pnl
        self._update_gross_pnl(closed_pnl)
        self._record_closed_trade(exit_bar, fill_price, prev, False,
                                  exit_date, exit_time, exit_name, exit_cat)
        self._reset_flat()

    def _reset_flat(self):
        """Reset all position/trade-tracking state to flat."""
        self.market_position = 0
        self.contracts = 0
        self.entry_price = 0.0
        self.avgentryprice = 0.0
        self.entry_bar = 0
        self.bars_since_entry = 0
        self.position_profit = 0.0
        self._breakeven_triggered = False
        self._peak_profit = 0.0
        self._entry_prices = []
        self.entry_date = 0
        self.entry_time = 0
        self.entry_name = ''
        self.entry_cat = 0

    def _compute_pnl(self, close_price):
        """Mark-to-market: compute open position PnL at current close price."""
        if self.market_position == 0 or self.entry_bar == 0:
            self.open_position_profit = 0.0
        else:
            diff = close_price - self.entry_price
            self.open_position_profit = diff * self.market_position * self.point_value * self.contracts

    def _check_money_management_stops(self, high, low, close_price, barnumber=None, open_price=None,
                                       date_str=0, time_str=0):
        """Close the position if the bar's intra-bar price path crosses any active
        money-management stop (StopLoss / ProfitTarget / BreakEven / PercentTrailing /
        DollarTrailing).

        Unified intra-bar PATH WALK (bar magnifier OFF). MultiCharts evaluates the
        protective orders on EVERY bar the position is open — INCLUDING the entry
        bar (a market entry that gaps into its stop-loss exits the same bar). It
        walks an assumed path: from the Open to whichever extreme (High or Low) is
        NEARER the open FIRST, then to the other extreme, then to the Close. The
        FIRST level the walked price crosses fires, and it fills at that level's
        price — EXCEPT when a level is already crossed at the Open (a gap), where it
        fills at the Open. As the path reaches the FAVORABLE extreme (high for a
        long, low for a short) the running peak ratchets to include this bar's
        favorable excursion, which arms BreakEven (>= be_amt) and PercentTrailing
        (>= trail_floor) and tightens the trail level for the remaining legs.

        Because every level is walked in price order, a tighter protective stop
        (e.g. a breakeven or trailing buy-stop sitting below the wider stop-loss on
        a short) correctly fires BEFORE the looser one — reproducing every GT2
        intra-session MM exit across the full capture range, e.g.:
          * bn=58886: short fills at open 21148.75, the high (21203.25) crosses the
                      +1000 stop-loss at 21198.75 on the ENTRY bar -> exit -1000.
          * bn=72536: short, open by the low ratchets the peak; the rise to the high
                      crosses entry (breakeven) BEFORE the wider stop-loss -> exit $0.
          * bn=39911/57194/16282: short trailing exits (peak ratchets at the low,
                      the high/close crosses the ratcheted buy-stop).
          * bn=2822:  short, high reached first with only the (sub-floor) prior peak
                      -> no trail; close stays below the stop -> MC holds (no exit).
          * bn=197611/129266: long trailing stop already crossed at the open (gap) ->
                      fills at the open, not the computed stop level.
        """
        if self.market_position == 0 or self.entry_bar == 0:
            return

        long_pos = self.market_position > 0
        pv = self.point_value
        pv_ct = pv * self.contracts
        entry = self.entry_price

        sl_amt = self._risk.get('stop_loss', 0)
        pt_amt = self._risk.get('profit_target', 0)
        be_amt = self._risk.get('breakeven', 0)
        pt_cfg = self._risk.get('percent_trailing', None)
        has_trail = bool(pt_cfg and isinstance(pt_cfg, (list, tuple))
                         and len(pt_cfg) == 2 and float(pt_cfg[1]) > 0)
        trail_floor = float(pt_cfg[0]) if has_trail else 0.0
        trail_pct = float(pt_cfg[1]) if has_trail else 0.0
        # SetDollarTrailing: exit once open profit gives back dt_amt from its
        # peak since entry. Armed from entry (the peak starts at 0), so before
        # any profit it sits dt_amt below entry like a stop-loss.
        dt_amt = self._risk.get('dollar_trailing', 0) or 0

        # Fall back to close when an extreme is unavailable (OHLC-less captures).
        o = open_price if (open_price is not None and open_price > 0) else close_price
        hi = high if high > 0 else close_price
        lo = low if low > 0 else close_price
        c = close_price

        # Fixed levels (snapped AWAY from the position, conservative).
        sl_price = (self._snap(entry - sl_amt / pv, 'down') if long_pos
                    else self._snap(entry + sl_amt / pv, 'up')) if (sl_amt and sl_amt > 0) else None
        pt_price = (self._snap(entry + pt_amt / pv, 'up') if long_pos
                    else self._snap(entry - pt_amt / pv, 'down')) if (pt_amt and pt_amt > 0) else None

        def _trail_price(peak):
            give = peak * trail_pct / 100.0
            base = (peak - give) / pv_ct
            return self._snap(entry + base, 'down') if long_pos else self._snap(entry - base, 'up')

        # Active levels for the given running peak, tagged with the price DIRECTION
        # that triggers them: profit-target on the favorable side, the protective
        # stops (stop-loss / breakeven / trailing) on the adverse side.
        def _levels(peak):
            out = []
            if pt_price is not None:
                out.append((pt_price, 'profit_target', long_pos))      # up-triggered iff long
            if sl_price is not None:
                out.append((sl_price, 'stop_loss', not long_pos))
            if be_amt and be_amt > 0 and peak >= be_amt:
                out.append((entry, 'breakeven', not long_pos))
            if has_trail and peak >= trail_floor:
                out.append((_trail_price(peak), 'trailing_stop', not long_pos))
            if dt_amt > 0:
                base = (peak - dt_amt) / pv_ct
                lv = self._snap(entry + base, 'down') if long_pos else self._snap(entry - base, 'up')
                out.append((lv, 'dollar_trailing', not long_pos))
            return out

        eps = 1e-9
        peak = self._peak_profit            # peak open-position profit from PRIOR bars
        best_pnl = ((hi if long_pos else lo) - entry) * self.market_position * pv_ct

        # (1) Gap: any level already crossed at the OPEN fills at the OPEN price.
        for lv, reason, up_trig in _levels(peak):
            if (up_trig and o >= lv - eps) or ((not up_trig) and o <= lv + eps):
                self._close_at_stop(o, reason, barnumber, fill_date=date_str, fill_time=time_str)
                return

        # (2) Walk Open -> nearer extreme -> farther extreme -> Close. On each
        #     monotonic leg, fire the NEAREST active level reachable in that
        #     direction; ratchet the peak on reaching the favorable extreme.
        fav_price = hi if long_pos else lo
        nearer_low = abs(o - lo) <= abs(o - hi)
        w1 = lo if nearer_low else hi
        w2 = hi if nearer_low else lo
        waypoints = [o, w1, w2, c]
        for i in range(3):
            s, e = waypoints[i], waypoints[i + 1]
            moving_up = e >= s
            seg_lo, seg_hi = (s, e) if moving_up else (e, s)
            reach = [(lv, reason) for lv, reason, up_trig in _levels(peak)
                     if up_trig == moving_up and seg_lo - eps <= lv <= seg_hi + eps]
            if reach:
                lv, reason = (min(reach, key=lambda x: x[0]) if moving_up
                              else max(reach, key=lambda x: x[0]))
                self._close_at_stop(lv, reason, barnumber, fill_date=date_str, fill_time=time_str)
                return
            if abs(e - fav_price) < eps:
                peak = max(peak, (e - entry) * self.market_position * pv_ct)

        # No stop fired: carry this bar's favorable excursion into the peak for the
        # NEXT bar's arming/trail level (MultiCharts uses the prior-bars peak).
        self._peak_profit = max(self._peak_profit, best_pnl)

        # SetExitOnClose — only triggers on the last bar (deferred for now)
        # EL's SetExitOnClose exits all positions on the close of the last bar.
        # Not implemented yet (requires knowing when it's the last bar).

    def _snap(self, price, direction='nearest'):
        """Snap a price to the instrument tick grid (MultiCharts order placement).

        MultiCharts aligns a protective stop to a tradable price by rounding it
        AWAY from the position (conservative): a long protective stop/trail sits
        below the market and is floored to the tick; a short protective stop/trail
        sits above the market and is ceiled. 'nearest' is used where direction is
        immaterial.
        """
        if not self.tick:
            return price
        q = price / self.tick
        if direction == 'down':
            import math
            n = math.floor(q + 1e-9)
        elif direction == 'up':
            import math
            n = math.ceil(q - 1e-9)
        else:
            n = round(q)
        return f32(n * self.tick)

    def _close_at_stop(self, price, reason, exit_bar=0, exit_date=0, exit_time=0,
                       exit_name='', exit_cat=0, fill_date=None, fill_time=None):
        """Close the current position at a stop price.

        The exit is logged in ``fills`` under ``exit_name`` (or ``reason``) at
        ``fill_date``/``fill_time`` when given, else ``exit_date``/``exit_time``.
        """
        islong = self.market_position > 0
        size = self.contracts
        prev_signed = self._signed_position()
        if self.market_position > 0:
            closed_pnl = (price - self.entry_price) * self.point_value * self.contracts
        elif self.market_position < 0:
            closed_pnl = (self.entry_price - price) * self.point_value * self.contracts
        else:
            return
        self.net_profit += closed_pnl
        self.total_trades += 1
        self.position_profit += closed_pnl
        self._update_gross_pnl(closed_pnl)
        self._record_closed_trade(exit_bar, price, size, islong,
                                  exit_date, exit_time, exit_name, exit_cat)
        self.market_position = 0
        self.contracts = 0
        self.entry_price = 0.0
        self.avgentryprice = 0.0
        self.entry_bar = 0
        self.bars_since_entry = 0
        self.open_position_profit = 0.0
        self.position_profit = 0.0
        self._breakeven_triggered = False
        self._peak_profit = 0.0
        self._entry_prices = []
        self.entry_date = 0
        self.entry_time = 0
        self.entry_name = ''
        self.entry_cat = 0
        self._record_fills(not islong, exit_name or reason,
                           exit_date if fill_date is None else fill_date,
                           exit_time if fill_time is None else fill_time,
                           price, prev_signed)

    def fill_pos_kwargs(self, d):
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

    @property
    def max_shares(self):
        """MaxShares / MaxShares(0): most contracts held while the current
        position is open; 0 when flat."""
        return self._pos_max_contracts if self.market_position != 0 else 0

    @property
    def open_entries_count(self):
        """OpenEntriesCount: entries of the current position with contracts still
        open (a FIFO exit that uses up an entry removes it); 0 when flat."""
        return len(self._entry_qtys) if self.market_position != 0 else 0

    def get_kwargs(self):
        """Return position kwargs to pass to strategy()."""
        d = {}
        self.fill_pos_kwargs(d)
        return d

    def get_mc_trades(self, v1=False):
        """Return list of MC-format trade dicts: order_no, type, signal, date, time, price, contracts.

        ``type`` is the order side: 'EntryLong' = buy side (Buy/BuyToCover),
        'ExitLong' = sell side (Sell/SellShort). See pl_oracle.fill_trade_types
        for the position-aware label. ``v1=True`` returns the pre-v2 log
        (no reversal open legs, no risk exits) for ``pl_oracle --digest-v1``."""
        trades = []
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

    def inject_trace(self, trace):
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

    def _inject_gta6_introspection(self, trace):
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
        trace['endtm'] = _el_to_ole(self.entry_date, self.entry_time) if in_pos else 0.0
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
            trace['ptentrydtm'] = _el_to_ole(t['entry_date'], t['entry_time'])
            trace['ptexitdtm'] = _el_to_ole(t['exit_date'], t['exit_time'])
            trace['ptentrycat'] = t['entry_cat']
            trace['ptexitcat'] = t['exit_cat']
            trace['ptentrynm'] = t['entry_name']
            trace['ptexitnm'] = t['exit_name']
            trace['exnm'] = t['exit_name']
            trace['exdate'] = t['exit_date']
            trace['extime'] = t['exit_time']
            trace['exdtm'] = _el_to_ole(t['exit_date'], t['exit_time'])
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

    def _inject_gta6_stats(self, trace):
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


from pl_transpiler.bar_loop import (  # noqa: E402,F401  (re-export)
    _POS_KWARGS,
    _DAILY_OHLC_KEYS,
    _code_has_const,
    run_bar_loop,
)
