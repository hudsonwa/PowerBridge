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


"""Money-management stop methods of `engine.FillEngine` (a mixin)."""

from pl_transpiler.runtime.pl_runtime import f32


class _StopsMixin:
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
