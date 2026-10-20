// Copyright 2026 Joshua Hudson
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! Native bar-loop and fill-engine primitives.
//!
//! This module deliberately contains no Python callbacks. A strategy executed
//! here must be a Rust implementation of [`NativeStrategy`], so the complete
//! tape stays on the Rust side of the FFI boundary.
//!
//! The shipped Python entry point does not select a strategy yet: its frozen
//! seven-array signature contains neither an EL program/AST nor a stable
//! strategy identifier (and Audit Ref 3 also needs Data2). The selected phase-2
//! strategy policy is therefore **fail-loud fallback**, not an implicit
//! hard-coded strategy. See `lib.rs` and `README.md`.

use std::collections::VecDeque;

// The frozen shared contract lives in `types`; the engine re-exports it here so
// the body below and the `tests` module (`use super::*`) keep naming the types
// directly. Nothing is redefined — see `crate::types` for every invariant.
pub(crate) use crate::types::*;

#[derive(Debug, Clone, Copy, Default)]
struct RiskSettings {
    stop_loss: Option<f64>,
    profit_target: Option<f64>,
    breakeven: Option<f64>,
    percent_trailing: Option<(f64, f64)>,
    dollar_trailing: Option<f64>,
    exit_on_close: bool,
}

impl RiskSettings {
    fn update(&mut self, update: RiskUpdate) {
        if let Some(value) = update.stop_loss {
            self.stop_loss = Some(value);
        }
        if let Some(value) = update.profit_target {
            self.profit_target = Some(value);
        }
        if let Some(value) = update.breakeven {
            self.breakeven = Some(value);
        }
        if let Some(value) = update.percent_trailing {
            self.percent_trailing = Some(value);
        }
        if let Some(value) = update.dollar_trailing {
            self.dollar_trailing = Some(value);
        }
        if let Some(value) = update.exit_on_close {
            self.exit_on_close = value;
        }
    }

    /// True iff any setting is armed, matching Python `_has_active_risk`: the
    /// three amount stops are armed only at a positive amount, trailing at a
    /// positive percent, and exit_on_close at true (mirrors the positive-value
    /// filter each consumer applies).
    fn is_active(&self) -> bool {
        matches!(self.stop_loss, Some(v) if v > 0.0)
            || matches!(self.profit_target, Some(v) if v > 0.0)
            || matches!(self.breakeven, Some(v) if v > 0.0)
            || matches!(self.dollar_trailing, Some(v) if v > 0.0)
            || matches!(self.percent_trailing, Some((_, pct)) if pct > 0.0)
            || self.exit_on_close
    }
}

#[derive(Debug, Clone)]
struct OnCloseOrder {
    order: Order,
    price: f64,
    bar_number: usize,
    date: i64,
    time: i64,
}

#[derive(Debug, Clone)]
pub(crate) struct FillEngine {
    instrument: Instrument,
    market_position: i8,
    contracts: isize,
    entry_price: f64,
    avg_entry_price: f64,
    entry_bar: usize,
    bars_since_entry: usize,
    open_position_profit: f64,
    position_profit: f64,
    net_profit: f64,
    gross_profit: f64,
    gross_loss: f64,
    total_trades: usize,
    max_contracts_held: isize,
    max_id_drawdown: f64,
    peak_equity: f64,
    peak_profit: f64,
    position_mfe_per_contract: f64,
    position_mae_per_contract: f64,
    entry_prices: VecDeque<f64>,
    pending_orders: Vec<Order>,
    on_close_pending: Vec<OnCloseOrder>,
    risk: RiskSettings,
    fills: Vec<Fill>,
    // Closed-position ledger (contract freeze). Empty in this pre-ledger
    // engine; the `accel/engine` module fills it via
    // `SessionEngine::record_closed_trade` and it feeds `KernelOutcome`.
    closed_positions: Vec<ClosedPosition>,
    // Fast-path instrumentation: flat, no-work bars whose before_bar
    // lifecycle was skipped. Mirrors the Python engine's `fast_path_hits`.
    fast_path_hits: usize,
}

impl FillEngine {
    pub(crate) fn new(instrument: Instrument) -> Self {
        Self {
            instrument,
            market_position: 0,
            contracts: 0,
            entry_price: 0.0,
            avg_entry_price: 0.0,
            entry_bar: 0,
            bars_since_entry: 0,
            open_position_profit: 0.0,
            position_profit: 0.0,
            net_profit: 0.0,
            gross_profit: 0.0,
            gross_loss: 0.0,
            total_trades: 0,
            max_contracts_held: 0,
            max_id_drawdown: 0.0,
            peak_equity: 0.0,
            peak_profit: 0.0,
            position_mfe_per_contract: 0.0,
            position_mae_per_contract: 0.0,
            entry_prices: VecDeque::new(),
            pending_orders: Vec::new(),
            on_close_pending: Vec::new(),
            risk: RiskSettings::default(),
            fills: Vec::new(),
            closed_positions: Vec::new(),
            fast_path_hits: 0,
        }
    }

    pub(crate) fn position(&self) -> PositionView {
        PositionView {
            market_position: self.market_position,
            contracts: self.contracts,
            entry_price: self.entry_price,
            avg_entry_price: self.avg_entry_price,
            bars_since_entry: self.bars_since_entry,
            exit_price: 0.0,
            bars_since_exit: 0,
            open_position_profit: self.open_position_profit,
            position_profit: self.position_profit,
            net_profit: self.net_profit,
            gross_profit: self.gross_profit,
            gross_loss: self.gross_loss,
            total_trades: self.total_trades,
            max_contracts_held: self.max_contracts_held,
            max_id_drawdown: self.max_id_drawdown,
        }
    }

    fn no_work_state(&self) -> bool {
        // Exact mirror of the Python engine's `no_work_state`: flat, no
        // next-bar or on-close order queued, and no armed money-management/
        // session risk. net_profit cannot have changed since the last drawdown
        // fold in this state (it mutates only via a fill or a session-rollover
        // close, both of which imply a non-empty queue or an armed exit_on_close).
        self.market_position == 0
            && self.pending_orders.is_empty()
            && self.on_close_pending.is_empty()
            && !self.risk.is_active()
    }

    fn before_bar(&mut self, bar: Bar, bar_number: usize) -> Result<(), KernelError> {
        if self.no_work_state() {
            self.fast_path_hits += 1;
            return Ok(());
        }
        self.execute_on_close_pending()?;
        self.execute_pending_fills(bar, bar_number)?;
        self.check_money_management_stops(bar, bar_number);
        self.compute_pnl(bar.close);
        self.update_excursion(bar.high, bar.low);
        if self.market_position != 0 && self.entry_bar > 0 {
            self.bars_since_entry = bar_number - self.entry_bar;
        }
        self.update_drawdown(bar, bar_number);
        Ok(())
    }

    fn after_bar(
        &mut self,
        output: StrategyOutput,
        bar: Bar,
        bar_number: usize,
    ) -> Result<(), KernelError> {
        let mut pending = Vec::new();
        for order in output.orders {
            if order.timing == Timing::ThisBar && order.order_type == OrderType::Close {
                self.on_close_pending.push(OnCloseOrder {
                    order,
                    // Python: f32(close_price) at queue time.
                    price: narrow_f32(bar.close),
                    bar_number,
                    date: bar.date,
                    time: bar.time,
                });
            } else {
                // Python `after_bar`: only this-bar-on-close orders are queued;
                // everything else (including a this-bar non-close order or a
                // next-timed close) falls through to `pending` and is silently
                // dropped at the next bar.
                pending.push(order);
            }
        }
        self.pending_orders = pending;
        self.risk
            .update(output.risk.resolve_points(self.instrument.point_value));
        self.compute_pnl(bar.close);
        Ok(())
    }

    fn execute_on_close_pending(&mut self) -> Result<(), KernelError> {
        let pending = std::mem::take(&mut self.on_close_pending);
        for queued in pending {
            // Python's this-bar-on-close path (`_fill_close_order`) treats a
            // zero/omitted quantity as one, unlike its next-bar qty=0 "exit
            // all" convention; otherwise it applies `int(raw_qty)`.
            let quantity = resolve_on_close_quantity(queued.order.quantity)?;
            let previous = self.contracts;
            self.apply_action(
                queued.order.action,
                quantity,
                queued.price,
                queued.bar_number,
            )?;
            let changed = (self.contracts - previous).unsigned_abs();
            let filled = if changed == 0 {
                quantity.max(0) as usize
            } else {
                changed
            };
            self.record_action_fills(
                queued.order.action,
                &queued.order.signal,
                queued.date,
                queued.time,
                queued.price,
                filled,
            );
        }
        Ok(())
    }

    fn execute_pending_fills(&mut self, bar: Bar, bar_number: usize) -> Result<(), KernelError> {
        if self.pending_orders.is_empty() {
            return Ok(());
        }
        let pending = std::mem::take(&mut self.pending_orders);
        let mut market = Vec::new();
        let mut conditional = Vec::new();

        for (sequence, order) in pending.into_iter().enumerate() {
            // Python `_execute_pending_fills` silently skips every order that is
            // not next-timed, has no numeric price, or is not market/limit/stop.
            if order.timing != Timing::NextBar {
                continue;
            }
            match order.order_type {
                OrderType::Market => market.push(order),
                OrderType::Limit | OrderType::Stop => {
                    let Some(price) = order.price else {
                        continue;
                    };
                    let buy_side = order.action.is_buy_side();
                    let (side_high, triggered, at_open, fill_price) =
                        match (order.order_type, buy_side) {
                            (OrderType::Limit, true) => (
                                false,
                                bar.low <= price,
                                bar.open <= price,
                                narrow_f32(bar.open.min(price)),
                            ),
                            (OrderType::Limit, false) => (
                                true,
                                bar.high >= price,
                                bar.open >= price,
                                narrow_f32(bar.open.max(price)),
                            ),
                            (OrderType::Stop, true) => (
                                true,
                                bar.high >= price,
                                bar.open >= price,
                                narrow_f32(bar.open.max(price)),
                            ),
                            (OrderType::Stop, false) => (
                                false,
                                bar.low <= price,
                                bar.open <= price,
                                narrow_f32(bar.open.min(price)),
                            ),
                            _ => unreachable!(),
                        };
                    if triggered {
                        conditional.push(ConditionalFill {
                            side_high,
                            at_open,
                            sequence,
                            order,
                            fill_price,
                        });
                    }
                }
                OrderType::Close => continue,
            }
        }

        // Market fills occur at the open before either intrabar extreme.
        for order in market {
            self.fill_at(order, bar.open, bar_number, bar.date, bar.time)?;
        }

        // Strict '<': an exactly centered open resolves LOW first.
        let high_first = (bar.high - bar.open) < (bar.open - bar.low);
        conditional.sort_by_key(|item| {
            (
                if item.at_open { 0usize } else { 1usize },
                if item.side_high == high_first {
                    0usize
                } else {
                    1usize
                },
                item.sequence,
            )
        });
        for item in conditional {
            self.fill_at(item.order, item.fill_price, bar_number, bar.date, bar.time)?;
        }
        Ok(())
    }

    fn fill_at(
        &mut self,
        order: Order,
        fill_price: f64,
        bar: usize,
        date: i64,
        time: i64,
    ) -> Result<(), KernelError> {
        // Python `_fill_at`: qty==0 is the "exit all" convention; otherwise
        // `int(raw_qty)` truncates toward zero and stays signed (0.4 -> 0
        // contracts, -3 -> 3 fills), never the truncated usize the old kernel
        // used to mint a contract from a fractional quantity.
        let quantity = self.resolve_fill_quantity(order.action, order.quantity)?;
        let previous = self.contracts;
        self.apply_action(order.action, quantity, fill_price, bar)?;
        let filled = (self.contracts - previous).unsigned_abs();
        self.record_action_fills(order.action, &order.signal, date, time, fill_price, filled);
        Ok(())
    }

    fn record_action_fills(
        &mut self,
        action: Action,
        signal: &str,
        date: i64,
        time: i64,
        price: f64,
        quantity: usize,
    ) {
        for _ in 0..quantity {
            self.fills.push(Fill {
                fill_type: action.python_fill_type(),
                signal: signal.to_owned(),
                date,
                time,
                price,
                contracts: 1,
            });
        }
    }

    fn apply_action(
        &mut self,
        action: Action,
        quantity: isize,
        fill_price: f64,
        bar: usize,
    ) -> Result<(), KernelError> {
        match action {
            Action::Buy => {
                if self.market_position < 0 {
                    self.close_short(fill_price);
                }
                self.open_or_add_long(quantity, fill_price, bar);
            }
            Action::SellShort => {
                if self.market_position > 0 {
                    self.close_long_fifo(self.contracts, fill_price);
                }
                self.open_or_add_short(quantity, fill_price, bar)?;
            }
            Action::Sell => {
                if self.market_position > 0 {
                    self.close_long_fifo(quantity, fill_price);
                }
            }
            Action::BuyToCover => {
                if self.market_position < 0 {
                    self.close_short(fill_price);
                }
            }
        }
        Ok(())
    }

    fn open_or_add_long(&mut self, quantity: isize, fill_price: f64, bar: usize) {
        // Python `_open_or_add_long` rounding points:
        //   fill = f32(fill)
        //   old_value = f32(entry * contracts)
        //   entry = f32((old_value + fill * qty) / new_contracts)
        // All arithmetic between those calls remains f64.
        let fill_price = narrow_f32(fill_price);
        let old_contracts = self.contracts;
        let old_value = narrow_f32(self.entry_price * old_contracts as f64);
        self.market_position = 1;
        self.contracts += quantity;
        // Python `[fill_price] * qty` extends nothing for qty <= 0.
        let added = quantity.max(0) as usize;
        self.entry_prices
            .extend(std::iter::repeat_n(fill_price, added));
        self.entry_price = if self.contracts > 0 {
            narrow_f32((old_value + fill_price * quantity as f64) / self.contracts as f64)
        } else {
            narrow_f32(fill_price)
        };
        self.avg_entry_price = self.entry_price;
        if old_contracts == 0 {
            self.entry_bar = bar;
            self.position_profit = 0.0;
            self.reset_position_stats();
        }
        self.bars_since_entry = 0;
        self.peak_profit = 0.0;
        self.max_contracts_held = self.max_contracts_held.max(self.contracts);
    }

    fn open_or_add_short(
        &mut self,
        quantity: isize,
        fill_price: f64,
        bar: usize,
    ) -> Result<(), KernelError> {
        // Same explicit f32 sites as Python `_open_or_add_short`, including its
        // `abs(contracts)` for the old size (short size is stored positive).
        let fill_price = narrow_f32(fill_price);
        let old_contracts = self.contracts;
        let old_value = narrow_f32(self.entry_price * old_contracts.unsigned_abs() as f64);
        self.market_position = -1;
        self.contracts = old_contracts.unsigned_abs() as isize + quantity;
        let added = quantity.max(0) as usize;
        self.entry_prices
            .extend(std::iter::repeat_n(fill_price, added));
        if self.contracts == 0 {
            // Python divides by the new size; a zero size (negative qty add)
            // raises ZeroDivisionError there. Fail loud rather than emit inf.
            return Err(KernelError::unsupported(
                "short add produced zero contracts (negative quantity)",
            ));
        }
        self.entry_price =
            narrow_f32((old_value + fill_price * quantity as f64) / self.contracts as f64);
        self.avg_entry_price = self.entry_price;
        if old_contracts == 0 {
            self.entry_bar = bar;
            self.reset_position_stats();
        }
        self.bars_since_entry = 0;
        self.position_profit = 0.0;
        self.peak_profit = 0.0;
        self.max_contracts_held = self.max_contracts_held.max(self.contracts);
        Ok(())
    }

    fn close_long_fifo(&mut self, quantity: isize, fill_price: f64) {
        let closed = quantity.min(self.contracts);
        let pnl = (fill_price - self.entry_price) * self.instrument.point_value * closed as f64;
        self.record_realized_pnl(pnl);
        self.contracts -= closed;
        for _ in 0..closed {
            self.entry_prices.pop_front();
        }
        if self.contracts > 0 {
            let total: f64 = self.entry_prices.iter().sum();
            // Python narrows only after the f64 sum/division.
            self.entry_price = narrow_f32(total / self.entry_prices.len() as f64);
            self.avg_entry_price = self.entry_price;
        } else {
            self.reset_flat();
        }
    }

    fn close_short(&mut self, fill_price: f64) {
        // Python `_close_short` explicitly narrows the fill before PnL.
        let fill_price = narrow_f32(fill_price);
        let pnl =
            (self.entry_price - fill_price) * self.instrument.point_value * self.contracts as f64;
        self.record_realized_pnl(pnl);
        self.reset_flat();
    }

    fn record_realized_pnl(&mut self, pnl: f64) {
        self.net_profit += pnl;
        self.position_profit += pnl;
        self.total_trades += 1;
        if pnl > 0.0 {
            self.gross_profit += pnl;
        } else if pnl < 0.0 {
            self.gross_loss += pnl;
        }
    }

    fn reset_position_stats(&mut self) {
        self.position_mfe_per_contract = 0.0;
        self.position_mae_per_contract = 0.0;
    }

    fn reset_flat(&mut self) {
        self.market_position = 0;
        self.contracts = 0;
        self.entry_price = 0.0;
        self.avg_entry_price = 0.0;
        self.entry_bar = 0;
        self.bars_since_entry = 0;
        self.position_profit = 0.0;
        self.open_position_profit = 0.0;
        self.peak_profit = 0.0;
        self.entry_prices.clear();
    }

    fn compute_pnl(&mut self, close: f64) {
        self.open_position_profit = if self.market_position == 0 || self.entry_bar == 0 {
            0.0
        } else {
            (close - self.entry_price)
                * self.market_position as f64
                * self.instrument.point_value
                * self.contracts as f64
        };
    }

    fn update_excursion(&mut self, high: f64, low: f64) {
        // Python `update_excursion` returns for any non-positive size.
        if self.market_position == 0 || self.contracts <= 0 {
            return;
        }
        let (favorable, adverse) = if self.market_position > 0 {
            (
                (high - self.entry_price) * self.instrument.point_value,
                (low - self.entry_price) * self.instrument.point_value,
            )
        } else {
            (
                (self.entry_price - low) * self.instrument.point_value,
                (self.entry_price - high) * self.instrument.point_value,
            )
        };
        self.position_mfe_per_contract = self.position_mfe_per_contract.max(favorable);
        self.position_mae_per_contract = self.position_mae_per_contract.min(adverse);
    }

    fn update_drawdown(&mut self, bar: Bar, bar_number: usize) {
        // Python/MultiCharts path assumption: nearer extreme first; tie -> low.
        let mut path = if (bar.high - bar.open) < (bar.open - bar.low) {
            vec![bar.open, bar.high, bar.low, bar.close]
        } else {
            vec![bar.open, bar.low, bar.high, bar.close]
        };
        if self.market_position != 0 && self.contracts > 0 {
            if self.entry_bar == bar_number {
                path = post_entry_walk(&path, self.entry_price);
            }
            for price in path {
                let equity = self.net_profit
                    + self.market_position as f64
                        * (price - self.entry_price)
                        * self.instrument.point_value
                        * self.contracts as f64;
                self.update_drawdown_equity(equity);
            }
        } else {
            self.update_drawdown_equity(self.net_profit);
        }
    }

    fn update_drawdown_equity(&mut self, equity: f64) {
        self.peak_equity = self.peak_equity.max(equity);
        let drawdown = self.peak_equity - equity;
        if drawdown > self.max_id_drawdown.abs() {
            // Python narrows only when a new max drawdown is stored.
            self.max_id_drawdown = narrow_f32(-drawdown);
        }
    }

    fn check_money_management_stops(&mut self, bar: Bar, _bar_number: usize) {
        if self.market_position == 0 || self.entry_bar == 0 {
            return;
        }

        let long = self.market_position > 0;
        let point_value = self.instrument.point_value;
        let point_value_contracts = point_value * self.contracts as f64;
        let entry = self.entry_price;

        let stop_loss = self
            .risk
            .stop_loss
            .filter(|amount| *amount > 0.0)
            .map(|amount| {
                if long {
                    self.snap(entry - amount / point_value, Snap::Down)
                } else {
                    self.snap(entry + amount / point_value, Snap::Up)
                }
            });
        let profit_target = self
            .risk
            .profit_target
            .filter(|amount| *amount > 0.0)
            .map(|amount| {
                if long {
                    self.snap(entry + amount / point_value, Snap::Up)
                } else {
                    self.snap(entry - amount / point_value, Snap::Down)
                }
            });
        let breakeven = self.risk.breakeven.filter(|amount| *amount > 0.0);
        let trailing = self
            .risk
            .percent_trailing
            .filter(|(_, percent)| *percent > 0.0);
        let dollar_trailing = self.risk.dollar_trailing.filter(|amount| *amount > 0.0);
        // Python's MM path alone substitutes Close for unavailable OHLC
        // values. Conditional orders and drawdown continue to use raw OHLC.
        let open = if bar.open > 0.0 { bar.open } else { bar.close };
        let high = if bar.high > 0.0 { bar.high } else { bar.close };
        let low = if bar.low > 0.0 { bar.low } else { bar.close };

        let levels = |engine: &Self, peak: f64| {
            let mut levels = Vec::with_capacity(4);
            if let Some(price) = profit_target {
                levels.push(Level {
                    price,
                    moves_up: long,
                });
            }
            if let Some(price) = stop_loss {
                levels.push(Level {
                    price,
                    moves_up: !long,
                });
            }
            if let Some(amount) = breakeven {
                if peak >= amount {
                    levels.push(Level {
                        price: entry,
                        moves_up: !long,
                    });
                }
            }
            if let Some((floor, percent)) = trailing {
                if peak >= floor {
                    let giveback = peak * percent / 100.0;
                    let offset = (peak - giveback) / point_value_contracts;
                    levels.push(Level {
                        price: if long {
                            engine.snap(entry + offset, Snap::Down)
                        } else {
                            engine.snap(entry - offset, Snap::Up)
                        },
                        moves_up: !long,
                    });
                }
            }
            if let Some(amount) = dollar_trailing {
                let offset = (peak - amount) / point_value_contracts;
                levels.push(Level {
                    price: if long {
                        engine.snap(entry + offset, Snap::Down)
                    } else {
                        engine.snap(entry - offset, Snap::Up)
                    },
                    moves_up: !long,
                });
            }
            levels
        };

        let mut peak = self.peak_profit;
        let favorable_price = if long { high } else { low };
        let best_pnl =
            (favorable_price - entry) * self.market_position as f64 * point_value_contracts;
        let epsilon = 1e-9;

        // A level already crossed at the open fills at the open.
        if levels(self, peak).iter().any(|level| {
            (level.moves_up && open >= level.price - epsilon)
                || (!level.moves_up && open <= level.price + epsilon)
        }) {
            self.close_at_stop(open);
            return;
        }

        let nearer_low = (open - low).abs() <= (open - high).abs();
        let first = if nearer_low { low } else { high };
        let second = if nearer_low { high } else { low };
        let waypoints = [open, first, second, bar.close];

        for segment in waypoints.windows(2) {
            let start = segment[0];
            let end = segment[1];
            let moving_up = end >= start;
            let low = start.min(end);
            let high = start.max(end);
            let reachable: Vec<Level> = levels(self, peak)
                .into_iter()
                .filter(|level| {
                    level.moves_up == moving_up
                        && level.price >= low - epsilon
                        && level.price <= high + epsilon
                })
                .collect();
            if !reachable.is_empty() {
                let selected = select_extreme_index(&reachable, moving_up);
                self.close_at_stop(reachable[selected].price);
                return;
            }
            if (end - favorable_price).abs() < epsilon {
                peak =
                    peak.max((end - entry) * self.market_position as f64 * point_value_contracts);
            }
        }

        self.peak_profit = self.peak_profit.max(best_pnl);
    }

    fn close_at_stop(&mut self, price: f64) {
        let pnl = if self.market_position > 0 {
            (price - self.entry_price) * self.instrument.point_value * self.contracts as f64
        } else {
            (self.entry_price - price) * self.instrument.point_value * self.contracts as f64
        };
        self.record_realized_pnl(pnl);
        // This intentionally mirrors the current Python FillEngine: MM and
        // end-of-session closures mutate position/PnL but are not appended to
        // `FillEngine.fills`. Explicit orders are the trade-list records.
        self.reset_flat();
    }

    /// Python `_fill_at` quantity semantics (`raw_qty == 0` is the "exit all"
    /// convention; otherwise `int(raw_qty)` truncates toward zero and stays
    /// signed). A non-finite quantity raises in Python (`int(float('nan'))` /
    /// `int(float('inf'))`), so the kernel fails loud there too.
    fn resolve_fill_quantity(&self, action: Action, raw_qty: f64) -> Result<isize, KernelError> {
        if raw_qty == 0.0 {
            // Python `_fill_at`: qty==0 means "the whole position" for CLOSING
            // verbs only. An entry sized off the current position doubled it
            // every bar (2**n fills, unbounded memory); entries default to 1.
            Ok(match action {
                Action::Sell | Action::BuyToCover => self.contracts.max(1),
                _ => 1,
            })
        } else if raw_qty.is_finite() {
            Ok(raw_qty as isize)
        } else {
            Err(KernelError::unsupported(format!(
                "non-finite order quantity {raw_qty}"
            )))
        }
    }

    fn snap(&self, price: f64, direction: Snap) -> f64 {
        if self.instrument.tick == 0.0 {
            return price;
        }
        let quotient = price / self.instrument.tick;
        let ticks = match direction {
            Snap::Down => (quotient + 1e-9).floor(),
            Snap::Up => (quotient - 1e-9).ceil(),
        };
        // Python `_snap` narrows only the final tick-grid product.
        narrow_f32(ticks * self.instrument.tick)
    }
}

/// Python `_fill_close_order` quantity semantics: a zero quantity means one
/// contract (there is no "exit all" convention on this-bar-on-close); any
/// other finite value is `int(raw_qty)`. Non-finite values raise in Python.
fn resolve_on_close_quantity(raw_qty: f64) -> Result<isize, KernelError> {
    if raw_qty == 0.0 {
        Ok(1)
    } else if raw_qty.is_finite() {
        Ok(raw_qty as isize)
    } else {
        Err(KernelError::unsupported(format!(
            "non-finite order quantity {raw_qty}"
        )))
    }
}

/// Index of the level Python's `min`/`max(reach, key=lambda x: x[0])` selects
/// within `_levels` construction order (profit-target, stop-loss, breakeven,
/// trailing). Python's `min`/`max` keep the FIRST extreme on a tie; a
/// stable-sorted last-element pick would invert the down-leg tie-break.
/// `levels` must be non-empty (callers guard with `!reachable.is_empty()`).
fn select_extreme_index(levels: &[Level], moving_up: bool) -> usize {
    let mut best = 0;
    for (index, level) in levels.iter().enumerate().skip(1) {
        let better = if moving_up {
            level.price < levels[best].price
        } else {
            level.price > levels[best].price
        };
        if better {
            best = index;
        }
    }
    best
}

#[derive(Debug)]
struct ConditionalFill {
    side_high: bool,
    at_open: bool,
    sequence: usize,
    order: Order,
    fill_price: f64,
}

#[derive(Debug, Clone, Copy)]
struct Level {
    price: f64,
    moves_up: bool,
}

#[derive(Debug, Clone, Copy)]
enum Snap {
    Down,
    Up,
}

fn post_entry_walk(path: &[f64], entry: f64) -> Vec<f64> {
    for (index, segment) in path.windows(2).enumerate() {
        if entry >= segment[0].min(segment[1]) && entry <= segment[0].max(segment[1]) {
            let mut result = Vec::with_capacity(path.len() - index);
            result.push(entry);
            result.extend_from_slice(&path[index + 1..]);
            return result;
        }
    }
    path.to_vec()
}

/// Whether to print `[accel]` diagnostics to stderr: set by the CLIs' `--verbose`
/// (`PB_ACCEL_VERBOSE=1`) or by `PB_DEBUG=1`. Off by default.
pub(crate) fn diagnostics_enabled() -> bool {
    ["PB_ACCEL_VERBOSE", "PB_DEBUG"]
        .iter()
        .any(|k| std::env::var(k).is_ok_and(|v| !v.is_empty() && v != "0"))
}

/// Run a Rust strategy over an entire tape without leaving native code.
pub(crate) fn run_native<S: NativeStrategy>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
) -> Result<KernelOutcome, KernelError> {
    let plan = strategy.skip_plan(tape);
    if !crate::kernel::diagnostics_enabled() {
        // [accel] diagnostics are opt-in (--verbose / PB_DEBUG).
    } else if !plan.proven {
        eprintln!("[accel] skip-region: full evaluation — {}", plan.reason);
    } else if plan.eval_from > 0 {
        eprintln!(
            "[accel] skip-region: skipped {} of {} bars ({})",
            plan.eval_from,
            tape.len(),
            plan.reason
        );
    }
    run_native_from(tape, instrument, strategy, plan.eval_from)
}

/// Run a strategy starting at `eval_from` (0 = the whole tape).
///
/// Prefix bars are not evaluated. Only legal when a skip-region proof has
/// shown those bars cannot emit an order (and warmup sessions cover live state).
pub(crate) fn run_native_from<S: NativeStrategy>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    eval_from: usize,
) -> Result<KernelOutcome, KernelError> {
    run_native_from_observing(tape, instrument, strategy, eval_from, |_, _, _, _| Ok(()))
}

/// Production loop with an optional post-`after_bar` observer.
///
/// The observer sees the same engine/strategy state the Python trace injects
/// after `after_bar` (on-close fills still queued, not yet applied). A no-op
/// observer is the measured production path. The audit-packet SPIKE is the
/// only in-tree observer; it must not allocate per bar on the no-op path.
pub(crate) fn run_native_from_observing<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    eval_from: usize,
    mut observe: F,
) -> Result<KernelOutcome, KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, PositionView, Bar, usize) -> Result<(), KernelError>,
{
    if eval_from > tape.len() {
        return Err(KernelError::input(format!(
            "eval_from {eval_from} exceeds tape length {}",
            tape.len()
        )));
    }
    let mut engine = FillEngine::new(instrument);
    let mut previous_date = None;
    let mut previous_close = 0.0;

    for index in eval_from..tape.len() {
        let bar_number = index + 1;
        let bar = tape.bar(index);

        if previous_date.is_some_and(|date| date != bar.date) && engine.risk.exit_on_close {
            // Same ordering as Python: an on-close order from the prior bar is
            // applied before SetExitOnClose flattens what remains.
            engine.execute_on_close_pending()?;
            if engine.market_position != 0 {
                engine.close_at_stop(previous_close);
            }
        }

        engine.before_bar(bar, bar_number)?;
        let output = strategy
            .on_bar(bar, bar_number, engine.position())
            .map_err(|error| KernelError::strategy(format!("bar {bar_number}: {error}")))?;
        engine.after_bar(output, bar, bar_number)?;
        observe(strategy, engine.position(), bar, index)?;
        previous_date = Some(bar.date);
        previous_close = bar.close;
    }

    let position = engine.position();
    Ok(KernelOutcome {
        fills: engine.fills,
        position,
        total_trades: engine.total_trades,
        max_id_drawdown: engine.max_id_drawdown,
        // The closed-position ledger (contract freeze output surface). The
        // pre-ledger `FillEngine` above never records a closed trade yet — the
        // `accel/engine` module wires `SessionEngine::record_closed_trade` into
        // the close paths, after which this is populated and
        // `closed_positions` becomes non-empty.
        closed_positions: engine.closed_positions,
        skipped_bars: eval_from,
        fast_path_hits: engine.fast_path_hits,
    })
}

/// Run a strategy over an already-sliced tape, reporting bars at the original
/// absolute indices.
///
/// The tape is the slice `full[offset..]` (see `run_native_from` for why the
/// prefix is safe to drop). The interpreter's per-bar bookkeeping (`bar_number`,
/// the Data2 alignment index) is RELATIVE to the slice so it reproduces the
/// full-tape loop exactly (a constant offset is invariant under every
/// difference the engine computes), while the observer receives the ABSOLUTE
/// 0-based index so a collector can stamp `bar_index`/`eval_from` correctly.
/// Only the swept `run_native_from_observing` loop observes; this offset loop is
/// the slice-before-marshal sweep seam.
pub(crate) fn run_native_from_offset<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    offset: usize,
    mut observe: F,
) -> Result<KernelOutcome, KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, PositionView, Bar, usize) -> Result<(), KernelError>,
{
    let mut engine = FillEngine::new(instrument);
    let mut previous_date = None;
    let mut previous_close = 0.0;

    for i in 0..tape.len() {
        let bar_number = i + 1;
        let bar = tape.bar(i);

        if previous_date.is_some_and(|date| date != bar.date) && engine.risk.exit_on_close {
            engine.execute_on_close_pending()?;
            if engine.market_position != 0 {
                engine.close_at_stop(previous_close);
            }
        }

        engine.before_bar(bar, bar_number)?;
        let output = strategy
            .on_bar(bar, bar_number, engine.position())
            .map_err(|error| KernelError::strategy(format!("bar {bar_number}: {error}")))?;
        engine.after_bar(output, bar, bar_number)?;
        observe(strategy, engine.position(), bar, offset + i)?;
        previous_date = Some(bar.date);
        previous_close = bar.close;
    }

    let position = engine.position();
    Ok(KernelOutcome {
        fills: engine.fills,
        position,
        total_trades: engine.total_trades,
        max_id_drawdown: engine.max_id_drawdown,
        closed_positions: engine.closed_positions,
        skipped_bars: offset,
        fast_path_hits: engine.fast_path_hits,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar(open: f64, high: f64, low: f64, close: f64, date: i64, time: i64) -> Bar {
        Bar {
            open,
            high,
            low,
            close,
            volume: 1.0,
            date,
            time,
        }
    }

    fn tape(bars: &[Bar]) -> BarTape {
        BarTape::from_columns(
            bars.iter().map(|b| b.open).collect(),
            bars.iter().map(|b| b.high).collect(),
            bars.iter().map(|b| b.low).collect(),
            bars.iter().map(|b| b.close).collect(),
            bars.iter().map(|b| b.volume).collect(),
            bars.iter().map(|b| b.date).collect(),
            bars.iter().map(|b| b.time).collect(),
        )
        .unwrap()
    }

    fn queue(engine: &mut FillEngine, orders: Vec<Order>, risk: RiskUpdate, bar: Bar, bn: usize) {
        engine
            .after_bar(StrategyOutput { orders, risk }, bar, bn)
            .unwrap();
    }

    fn enter_long(engine: &mut FillEngine) {
        let signal_bar = bar(100.0, 100.0, 100.0, 100.0, 1240102, 930);
        queue(
            engine,
            vec![Order::market(Action::Buy, 1.0, "LE")],
            RiskUpdate::default(),
            signal_bar,
            1,
        );
        engine
            .before_bar(bar(100.0, 100.0, 100.0, 100.0, 1240102, 931), 2)
            .unwrap();
    }

    #[test]
    fn tape_rejects_length_mismatch() {
        let error = BarTape::from_columns(
            vec![1.0],
            vec![],
            vec![1.0],
            vec![1.0],
            vec![1.0],
            vec![1240102],
            vec![930],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "invalid accel input: column length mismatch: open has 1 bars but high has 0"
        );
    }

    #[test]
    fn f32_is_explicit_not_every_operation() {
        assert_eq!(narrow_f32(16_777_217.0), 16_777_216.0);

        let mut engine = FillEngine::new(Instrument::default());
        engine.open_or_add_long(25, 13_436_425.0, 1);
        engine.open_or_add_long(3, 84_743_376.0, 2);

        // Python's exact f32 call-site sequence yields 21076456. A naive f32
        // multiply/add/divide pipeline yields 21076454 instead.
        assert_eq!(engine.entry_price, 21_076_456.0);
        let old_value = narrow_f32(13_436_425.0 * 25.0);
        let python_sites = narrow_f32((old_value + narrow_f32(84_743_376.0) * 3.0) / 28.0);
        let naive =
            narrow_f32(narrow_f32(old_value + narrow_f32(narrow_f32(84_743_376.0) * 3.0)) / 28.0);
        assert_eq!(engine.entry_price, python_sites);
        assert_eq!(naive, 21_076_454.0);
        assert_ne!(python_sites, naive);
    }

    #[test]
    fn native_loop_queues_next_bar_market_fill() {
        let bars = tape(&[
            bar(10.0, 10.0, 10.0, 10.0, 1240102, 930),
            bar(11.1250001, 12.0, 11.0, 11.5, 1240102, 931),
        ]);
        let mut strategy = |_bar: Bar, bn: usize, _position: PositionView| {
            Ok(StrategyOutput {
                orders: if bn == 1 {
                    vec![Order::market(Action::Buy, 1.0, "LE")]
                } else {
                    vec![]
                },
                risk: RiskUpdate::default(),
            })
        };
        let result = run_native(&bars, Instrument::default(), &mut strategy).unwrap();

        assert_eq!(result.fills.len(), 1);
        assert_eq!(result.fills[0].fill_type, "EntryLong");
        assert_eq!(result.fills[0].date, 1240102);
        assert_eq!(result.fills[0].time, 931);
        // Fill list keeps the raw market open; position state narrows at the
        // explicit `_open_or_add_long` f32 site.
        assert_eq!(result.fills[0].price, 11.1250001);
        assert_eq!(result.position.entry_price, narrow_f32(11.1250001));
        assert_eq!(result.max_id_drawdown, -10.0);
    }

    #[test]
    fn conditional_bracket_uses_low_first_on_tie() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        let signal = bar(100.0, 100.0, 100.0, 100.0, 1240102, 932);
        queue(
            &mut engine,
            vec![
                Order::conditional(Action::Sell, OrderType::Limit, 0.0, "PT", 105.0),
                Order::conditional(Action::Sell, OrderType::Stop, 0.0, "SL", 95.0),
            ],
            RiskUpdate::default(),
            signal,
            2,
        );
        engine
            .before_bar(bar(100.0, 106.0, 94.0, 100.0, 1240102, 933), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.fills.len(), 2);
        assert_eq!(engine.fills[1].signal, "SL");
        assert_eq!(engine.fills[1].price, 95.0);
    }

    #[test]
    fn conditional_gap_fills_at_open_before_path() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        queue(
            &mut engine,
            vec![Order::conditional(
                Action::Sell,
                OrderType::Stop,
                0.0,
                "SL_GAP",
                95.0,
            )],
            RiskUpdate::default(),
            bar(100.0, 100.0, 100.0, 100.0, 1240102, 932),
            2,
        );
        engine
            .before_bar(bar(94.0, 96.0, 90.0, 92.0, 1240102, 933), 3)
            .unwrap();

        assert_eq!(engine.fills[1].signal, "SL_GAP");
        assert_eq!(engine.fills[1].price, 94.0);
    }

    #[test]
    fn this_bar_close_is_applied_next_bar_with_signal_metadata() {
        let mut engine = FillEngine::new(Instrument::default());
        let mut order = Order::market(Action::Buy, 1.0, "LE_CLOSE");
        order.order_type = OrderType::Close;
        order.timing = Timing::ThisBar;
        queue(
            &mut engine,
            vec![order],
            RiskUpdate::default(),
            bar(10.0, 11.0, 9.0, 10.1250001, 1240102, 930),
            1,
        );
        assert!(engine.fills.is_empty());

        engine
            .before_bar(bar(20.0, 20.0, 20.0, 20.0, 1240103, 931), 2)
            .unwrap();
        assert_eq!(engine.fills.len(), 1);
        assert_eq!(engine.fills[0].date, 1240102);
        assert_eq!(engine.fills[0].time, 930);
        assert_eq!(engine.fills[0].price, narrow_f32(10.1250001));
    }

    #[test]
    fn mm_tie_walk_hits_long_stop_before_target() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.stop_loss = Some(100.0);
        engine.risk.profit_target = Some(100.0);
        engine
            .before_bar(bar(100.0, 106.0, 94.0, 100.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.net_profit, -100.0);
        // Matches current Python FillEngine: MM closes are state changes, not
        // explicit-order records in `fills`.
        assert_eq!(engine.fills.len(), 1);
    }

    #[test]
    fn mm_near_high_walk_hits_long_target_first() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.stop_loss = Some(100.0);
        engine.risk.profit_target = Some(100.0);
        engine
            .before_bar(bar(104.0, 106.0, 94.0, 100.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.net_profit, 100.0);
    }

    #[test]
    fn mm_breakeven_arms_at_favorable_extreme() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.breakeven = Some(100.0);
        engine
            .before_bar(bar(100.0, 110.0, 99.0, 99.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.net_profit, 0.0);
    }

    #[test]
    fn mm_percent_trail_ratchets_inside_bar() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.percent_trailing = Some((100.0, 50.0));
        engine
            .before_bar(bar(100.0, 110.0, 99.0, 104.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.net_profit, 100.0);
    }

    #[test]
    fn mm_dollar_trail_ratchets_inside_bar() {
        // Peak $200 at the high (110) puts the $60 trail at 107; the drop to
        // the close crosses it.
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.dollar_trailing = Some(60.0);
        engine
            .before_bar(bar(100.0, 110.0, 99.0, 104.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.net_profit, 140.0);
    }

    #[test]
    fn mm_dollar_trail_acts_as_stop_before_any_profit() {
        // With no favorable excursion yet the trail sits $60 (3 points) below
        // entry, so a gap open below it fills at the open.
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.dollar_trailing = Some(60.0);
        engine
            .before_bar(bar(96.0, 97.0, 95.0, 96.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.net_profit, -80.0);
    }

    #[test]
    fn mm_nonpositive_ohlc_falls_back_to_close() {
        let mut engine = FillEngine::new(Instrument::default());
        enter_long(&mut engine);
        engine.risk.stop_loss = Some(100.0);
        engine
            .before_bar(bar(0.0, 0.0, 0.0, 100.0, 1240102, 932), 3)
            .unwrap();

        assert_eq!(engine.market_position, 1);
        assert_eq!(engine.net_profit, 0.0);
    }

    #[test]
    fn session_rollover_exit_runs_inside_native_loop() {
        let bars = tape(&[
            bar(10.0, 10.0, 10.0, 10.0, 1240102, 930),
            bar(11.0, 12.0, 11.0, 12.0, 1240102, 931),
            bar(13.0, 13.0, 13.0, 13.0, 1240103, 930),
        ]);
        let mut strategy = |_bar: Bar, bn: usize, _position: PositionView| {
            Ok(StrategyOutput {
                orders: if bn == 1 {
                    vec![Order::market(Action::Buy, 1.0, "LE")]
                } else {
                    vec![]
                },
                risk: RiskUpdate {
                    exit_on_close: Some(true),
                    ..RiskUpdate::default()
                },
            })
        };
        let result = run_native(&bars, Instrument::default(), &mut strategy).unwrap();

        assert_eq!(result.position.market_position, 0);
        assert_eq!(result.position.net_profit, 20.0);
        assert_eq!(result.total_trades, 1);
    }

    #[test]
    fn price_less_conditional_is_dropped_like_python() {
        // A limit/stop whose index-5 slot is non-numeric is
        // silently DROPPED by Python (`engine.py:_execute_pending_fills`,
        // `price is None` -> continue). The old kernel aborted; this asserts
        // the Python behaviour the reference actually exhibits.
        let mut engine = FillEngine::new(Instrument::default());
        engine
            .after_bar(
                StrategyOutput {
                    orders: vec![Order {
                        action: Action::Buy,
                        order_type: OrderType::Limit,
                        quantity: 1.0,
                        timing: Timing::NextBar,
                        signal: "PT_NOPRICE".to_owned(),
                        price: None,
                    }],
                    risk: RiskUpdate::default(),
                },
                bar(1.0, 1.0, 1.0, 1.0, 1240102, 930),
                1,
            )
            .unwrap();
        // Next bar would satisfy a limit below the low; Python still drops it.
        engine
            .before_bar(bar(2.0, 3.0, 1.0, 2.0, 1240102, 931), 2)
            .unwrap();
        assert_eq!(engine.fills.len(), 0);
        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.contracts, 0);
    }

    #[test]
    fn next_bar_close_order_is_dropped_like_python() {
        // A `close`-type order with 'next' timing falls through
        // every branch of Python's `_execute_pending_fills` and is silently
        // dropped. The old kernel aborted; this asserts the Python behaviour.
        let mut engine = FillEngine::new(Instrument::default());
        let mut order = Order::market(Action::Buy, 1.0, "LE_CLOSE_NEXT");
        order.order_type = OrderType::Close;
        queue(
            &mut engine,
            vec![order],
            RiskUpdate::default(),
            bar(1.0, 1.0, 1.0, 1.0, 1240102, 930),
            1,
        );
        engine
            .before_bar(bar(2.0, 3.0, 1.0, 2.0, 1240102, 931), 2)
            .unwrap();
        assert_eq!(engine.fills.len(), 0);
        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.contracts, 0);
    }

    #[test]
    fn this_bar_non_close_order_is_dropped_like_python() {
        // Python `after_bar` only queues this-bar-on-close; a this-bar order
        // of any other type falls through to `pending` and is dropped at the
        // next bar (`timing != 'next'` -> continue). No abort.
        let mut engine = FillEngine::new(Instrument::default());
        let mut order = Order::market(Action::Buy, 1.0, "LE_THIS_MARKET");
        order.timing = Timing::ThisBar;
        queue(
            &mut engine,
            vec![order],
            RiskUpdate::default(),
            bar(1.0, 1.0, 1.0, 1.0, 1240102, 930),
            1,
        );
        engine
            .before_bar(bar(2.0, 3.0, 1.0, 2.0, 1240102, 931), 2)
            .unwrap();
        assert_eq!(engine.fills.len(), 0);
        assert_eq!(engine.market_position, 0);
    }

    #[test]
    fn fractional_quantity_yields_zero_contracts_and_zero_fills() {
        // Exact repro: `Buy (Risk / StopDistance) contracts`
        // evaluating to 0.4. Python `_fill_at`: 0.4 != 0 -> qty = int(0.4) = 0
        // -> phantom position (market_position 1, contracts 0) and ZERO fill
        // records. The old `usize` kernel received a truncated 0 and mistook
        // it for an exit-all, minting 1 contract and 1 fill. This pins the
        // Python behaviour end-to-end through `run_native`.
        let bars = tape(&[
            bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
            bar(100.0, 100.0, 100.0, 100.0, 1240102, 931),
        ]);
        let mut strategy = |_bar: Bar, bn: usize, _position: PositionView| {
            Ok(StrategyOutput {
                orders: if bn == 1 {
                    vec![Order::market(Action::Buy, 0.4, "LE_FRAC")]
                } else {
                    vec![]
                },
                risk: RiskUpdate::default(),
            })
        };
        let result = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
        assert_eq!(result.position.market_position, 1);
        assert_eq!(result.position.contracts, 0);
        assert_eq!(result.position.entry_price, 100.0);
        assert!(
            result.fills.is_empty(),
            "a 0.4-contract buy must emit zero fill records"
        );
    }

    #[test]
    fn quantity_is_signed_and_truncates_toward_zero() {
        // Probe table (Python `_fill_at`, single 'buy market <qty>'
        // from flat, fill at 100.0): 1 -> ct 1/fills 1; 0 -> ct 1/fills 1
        // (exit-all fallback from flat); 0.4 -> ct 0/fills 0; 2.7 -> ct 2/
        // fills 2; -3 -> ct -3/fills 3.
        let cases: [(f64, isize, usize); 5] = [
            (1.0, 1, 1),
            (0.0, 1, 1),
            (0.4, 0, 0),
            (2.7, 2, 2),
            (-3.0, -3, 3),
        ];
        for (qty, expected_contracts, expected_fills) in cases {
            let mut engine = FillEngine::new(Instrument::default());
            queue(
                &mut engine,
                vec![Order::market(Action::Buy, qty, "LE_TABLE")],
                RiskUpdate::default(),
                bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
                1,
            );
            engine
                .before_bar(bar(100.0, 100.0, 100.0, 100.0, 1240102, 931), 2)
                .unwrap();
            assert_eq!(engine.market_position, 1, "qty {qty}: market_position");
            assert_eq!(engine.contracts, expected_contracts, "qty {qty}: contracts");
            assert_eq!(engine.entry_price, 100.0, "qty {qty}: entry_price");
            assert_eq!(engine.fills.len(), expected_fills, "qty {qty}: fills");
            assert_eq!(engine.entry_bar, 2, "qty {qty}: entry_bar");
        }
    }

    #[test]
    fn mm_down_leg_selects_first_maximum_on_tie() {
        // Python's `max(reach, key=price)` keeps the FIRST
        // maximum in `_levels` construction order (profit-target, stop-loss,
        // breakeven, trailing); a stable-sorted last-element pick would invert
        // the down-leg tie-break. The tied records share a price, so the
        // selection is unobservable through `close_at_stop` today, but the ledger's
        // exit-reason introspection would make it a real divergence.
        let down = vec![
            Level {
                price: 100.0,
                moves_up: false,
            }, // profit_target, constructed first
            Level {
                price: 100.0,
                moves_up: false,
            }, // stop_loss, constructed second
        ];
        assert_eq!(select_extreme_index(&down, false), 0);
        // Python's `min` keeps the first minimum on the up leg too.
        let up = vec![
            Level {
                price: 95.0,
                moves_up: true,
            },
            Level {
                price: 95.0,
                moves_up: true,
            },
        ];
        assert_eq!(select_extreme_index(&up, true), 0);
        // Distinct prices still select the true extreme.
        let mixed = vec![
            Level {
                price: 80.0,
                moves_up: false,
            },
            Level {
                price: 120.0,
                moves_up: false,
            },
            Level {
                price: 90.0,
                moves_up: false,
            },
        ];
        assert_eq!(select_extreme_index(&mixed, false), 1);
        assert_eq!(select_extreme_index(&mixed, true), 0);
    }

    #[test]
    fn strategy_error_is_not_swallowed() {
        let bars = tape(&[bar(1.0, 1.0, 1.0, 1.0, 1240102, 930)]);
        let mut strategy = |_bar: Bar, _bn: usize, _position: PositionView| {
            Err(KernelError::strategy("deliberate"))
        };
        let error = run_native(&bars, Instrument::default(), &mut strategy).unwrap_err();
        assert_eq!(
            error.to_string(),
            "native strategy failed: bar 1: native strategy failed: deliberate"
        );
    }

    #[test]
    fn short_market_actions_are_stateful_and_recorded() {
        let mut engine = FillEngine::new(Instrument::default());
        queue(
            &mut engine,
            vec![Order::market(Action::SellShort, 2.0, "SE")],
            RiskUpdate::default(),
            bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
            1,
        );
        engine
            .before_bar(bar(99.0, 100.0, 98.0, 99.0, 1240102, 931), 2)
            .unwrap();
        assert_eq!(engine.market_position, -1);
        assert_eq!(engine.contracts, 2);
        assert_eq!(engine.fills.len(), 2);
        assert_eq!(engine.fills[0].fill_type, "ExitLong");

        queue(
            &mut engine,
            vec![Order::market(Action::BuyToCover, 0.0, "SX")],
            RiskUpdate::default(),
            bar(99.0, 99.0, 99.0, 99.0, 1240102, 931),
            2,
        );
        engine
            .before_bar(bar(98.0, 98.0, 98.0, 98.0, 1240102, 932), 3)
            .unwrap();
        assert_eq!(engine.market_position, 0);
        assert_eq!(engine.fills.len(), 4);
        assert_eq!(engine.fills[2].fill_type, "EntryLong");
    }

    #[test]
    fn no_work_fastpath_counts_real_skips() {
        // The active (FFI) engine must count flat no-work bars and expose the
        // counter on KernelOutcome, while staying byte-identical on fills.
        let bars = tape(&[
            bar(100.0, 101.0, 99.0, 100.5, 1240102, 930),
            bar(100.5, 101.5, 99.5, 101.0, 1240102, 931),
            bar(101.0, 102.0, 100.0, 101.5, 1240102, 932),
            bar(101.5, 102.5, 100.5, 102.0, 1240102, 933),
        ]);
        let mut strategy = |_bar: Bar, bn: usize, position: PositionView| {
            Ok(StrategyOutput {
                orders: if bn == 1 {
                    vec![Order::market(Action::Buy, 1.0, "LE")]
                } else if bn == 2 && position.market_position == 1 {
                    vec![Order::market(Action::Sell, 0.0, "LX")]
                } else {
                    vec![]
                },
                risk: RiskUpdate::default(),
            })
        };
        let result = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
        // bars 0 and 3 are flat no-work; 1 (entry) and 2 (exit) are fills.
        assert_eq!(result.fast_path_hits, 2);
        assert_eq!(result.fills.len(), 2);
        assert_eq!(result.position.market_position, 0);
    }
}
