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

use std::collections::VecDeque;

use crate::types::{
    narrow_f32, Action, Bar, ClosedPosition, Config, Fill, Instrument, KernelError, Order,
    OrderType, PositionAggregates, PositionView, RiskUpdate, SessionEngine, SessionStats,
    StrategyOutput, Timing,
};

#[derive(Debug, Clone, Copy, Default)]
struct RiskSettings {
    stop_loss: Option<f64>,
    profit_target: Option<f64>,
    breakeven: Option<f64>,
    percent_trailing: Option<(f64, f64)>,
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
        if let Some(value) = update.exit_on_close {
            self.exit_on_close = value;
        }
    }

    /// True iff any setting is armed, matching Python `_has_active_risk`.
    fn is_active(&self) -> bool {
        matches!(self.stop_loss, Some(v) if v > 0.0)
            || matches!(self.profit_target, Some(v) if v > 0.0)
            || matches!(self.breakeven, Some(v) if v > 0.0)
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

#[derive(Debug, Clone, Copy)]
struct Level {
    price: f64,
    moves_up: bool,
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
enum Snap {
    Down,
    Up,
}

/// Stateful native implementation of the frozen fill/session boundary.
///
/// Contract quantities remain signed from `Order.quantity` through position
/// mutation. A non-negative magnitude is needed only as a signed loop bound
/// when emitting one `Fill` per changed contract; it is never cast to an
/// unsigned position size.
#[derive(Debug, Clone)]
pub(crate) struct FillEngine {
    instrument: Instrument,
    config: Option<Config>,
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
    entry_prices: VecDeque<f64>,
    pending_orders: Vec<Order>,
    on_close_pending: Vec<OnCloseOrder>,
    risk: RiskSettings,
    fills: Vec<Fill>,
    closed_positions: Vec<ClosedPosition>,
    position_aggregates: PositionAggregates,
    session_stats: SessionStats,
    entry_date: i64,
    entry_time: i64,
    entry_name: String,
    entry_category: i64,
    // Fast-path instrumentation (#30): flat, no-work bars whose before_bar was
    // skipped. Mirrors the Python engine's `fast_path_hits` and kernel.rs.
    fast_path_hits: usize,
}

impl FillEngine {
    pub(crate) fn new(instrument: Instrument) -> Self {
        Self::new_inner(instrument, None)
    }

    pub(crate) fn from_config(config: &Config) -> Self {
        Self::new_inner(config.into(), Some(config.clone()))
    }

    fn new_inner(instrument: Instrument, config: Option<Config>) -> Self {
        Self {
            instrument,
            config,
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
            entry_prices: VecDeque::new(),
            pending_orders: Vec::new(),
            on_close_pending: Vec::new(),
            risk: RiskSettings::default(),
            fills: Vec::new(),
            closed_positions: Vec::new(),
            position_aggregates: PositionAggregates {
                entries: 0,
                max_contracts: 0,
                mfe_pc: 0.0,
                mae_pc: 0.0,
            },
            session_stats: SessionStats {
                n_win: 0,
                n_loss: 0,
                n_even: 0,
                largest_win: 0.0,
                largest_loss: 0.0,
                total_bars_win: 0,
                total_bars_loss: 0,
                total_bars_even: 0,
                cur_win_run: 0,
                cur_loss_run: 0,
                max_win_run: 0,
                max_loss_run: 0,
            },
            entry_date: 0,
            entry_time: 0,
            entry_name: String::new(),
            entry_category: 0,
            fast_path_hits: 0,
        }
    }

    pub(crate) fn config(&self) -> Option<&Config> {
        self.config.as_ref()
    }

    pub(crate) fn exit_on_close(&self) -> bool {
        self.risk.exit_on_close
    }

    pub(crate) fn fast_path_hits(&self) -> usize {
        self.fast_path_hits
    }

    /// Fold a strategy risk update without queueing orders.
    ///
    /// Seeded-prefix bars must keep money-management state (`SetStopLoss`,
    /// `SetExitOnClose`, ...) while fills stay suppressed.
    pub(crate) fn apply_risk(&mut self, update: RiskUpdate) {
        self.risk
            .update(update.resolve_points(self.instrument.point_value));
    }

    pub(crate) fn into_ledgers(self) -> (Vec<Fill>, Vec<ClosedPosition>) {
        (self.fills, self.closed_positions)
    }

    pub(crate) fn total_trades(&self) -> usize {
        self.total_trades
    }

    pub(crate) fn max_id_drawdown(&self) -> f64 {
        self.max_id_drawdown
    }

    pub(crate) fn entry_date(&self) -> i64 {
        self.entry_date
    }

    pub(crate) fn entry_time(&self) -> i64 {
        self.entry_time
    }

    pub(crate) fn entry_name(&self) -> &str {
        &self.entry_name
    }

    pub(crate) fn entry_category(&self) -> i64 {
        self.entry_category
    }

    pub(crate) fn execute_on_close_pending(&mut self) -> Result<(), KernelError> {
        let pending = std::mem::take(&mut self.on_close_pending);
        for queued in pending {
            let quantity = resolve_quantity(queued.order.quantity, QuantityMode::OnClose, 0)?;
            let previous = self.contracts;
            self.apply_action(
                queued.order.action,
                quantity,
                queued.price,
                queued.bar_number,
                queued.date,
                queued.time,
                &queued.order.signal,
                3,
            )?;
            let changed = signed_abs(
                self.contracts.checked_sub(previous).ok_or_else(|| {
                    KernelError::unsupported("contract delta overflow while filling close order")
                })?,
                "close-order contract delta",
            )?;
            let filled = if changed == 0 { quantity } else { changed };
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

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn close_for_session(
        &mut self,
        price: f64,
        exit_bar: usize,
        exit_date: i64,
        exit_time: i64,
        exit_name: &str,
        exit_category: i64,
    ) -> Result<(), KernelError> {
        self.close_at_stop(
            price,
            exit_bar,
            exit_date,
            exit_time,
            exit_name,
            exit_category,
        )
    }

    pub(crate) fn no_work_state(&self) -> bool {
        // Exact mirror of the Python engine's `no_work_state` (#30).
        self.market_position == 0
            && self.pending_orders.is_empty()
            && self.on_close_pending.is_empty()
            && !self.risk.is_active()
    }

    fn before_bar_impl(&mut self, bar: Bar, bar_number: usize) -> Result<(), KernelError> {
        if self.no_work_state() {
            self.fast_path_hits += 1;
            return Ok(());
        }
        bar_number_i64(bar_number)?;
        self.execute_on_close_pending()?;
        self.execute_pending_fills(bar, bar_number)?;
        self.check_money_management_stops(bar, bar_number)?;
        self.compute_pnl(bar.close);
        self.update_excursion(bar.high, bar.low);
        if self.market_position != 0 && self.entry_bar > 0 {
            self.bars_since_entry = bar_number.checked_sub(self.entry_bar).ok_or_else(|| {
                KernelError::input(format!(
                    "bar number {bar_number} precedes entry bar {}",
                    self.entry_bar
                ))
            })?;
        }
        self.update_drawdown(bar, bar_number);
        Ok(())
    }

    fn after_bar_impl(
        &mut self,
        output: StrategyOutput,
        bar: Bar,
        bar_number: usize,
    ) -> Result<(), KernelError> {
        bar_number_i64(bar_number)?;
        let mut pending = Vec::new();
        for order in output.orders {
            if order.timing == Timing::ThisBar && order.order_type == OrderType::Close {
                self.on_close_pending.push(OnCloseOrder {
                    order,
                    price: narrow_f32(bar.close),
                    bar_number,
                    date: bar.date,
                    time: bar.time,
                });
            } else {
                pending.push(order);
            }
        }
        self.pending_orders = pending;
        self.apply_risk(output.risk);
        self.compute_pnl(bar.close);
        Ok(())
    }

    fn execute_pending_fills(&mut self, bar: Bar, bar_number: usize) -> Result<(), KernelError> {
        if self.pending_orders.is_empty() {
            return Ok(());
        }

        let pending = std::mem::take(&mut self.pending_orders);
        let mut market_orders = Vec::new();
        let mut conditional_orders = Vec::new();
        for (sequence, order) in pending.into_iter().enumerate() {
            if order.timing != Timing::NextBar {
                continue;
            }
            match order.order_type {
                OrderType::Market => market_orders.push(order),
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
                                narrow_f32(python_min(bar.open, price)),
                            ),
                            (OrderType::Limit, false) => (
                                true,
                                bar.high >= price,
                                bar.open >= price,
                                narrow_f32(python_max(bar.open, price)),
                            ),
                            (OrderType::Stop, true) => (
                                true,
                                bar.high >= price,
                                bar.open >= price,
                                narrow_f32(python_max(bar.open, price)),
                            ),
                            (OrderType::Stop, false) => (
                                false,
                                bar.low <= price,
                                bar.open <= price,
                                narrow_f32(python_min(bar.open, price)),
                            ),
                            _ => unreachable!(),
                        };
                    if triggered {
                        conditional_orders.push(ConditionalFill {
                            side_high,
                            at_open,
                            sequence,
                            order,
                            fill_price,
                        });
                    }
                }
                OrderType::Close => {}
            }
        }

        for order in market_orders {
            self.fill_at(order, bar.open, bar_number, bar.date, bar.time)?;
        }

        let high_first = (bar.high - bar.open) < (bar.open - bar.low);
        conditional_orders.sort_by_key(|item| {
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
        for item in conditional_orders {
            self.fill_at(item.order, item.fill_price, bar_number, bar.date, bar.time)?;
        }
        Ok(())
    }

    fn fill_at(
        &mut self,
        order: Order,
        fill_price: f64,
        bar_number: usize,
        date: i64,
        time: i64,
    ) -> Result<(), KernelError> {
        let quantity =
            resolve_quantity(order.quantity, QuantityMode::NextBar, self.contracts.max(1))?;
        let previous = self.contracts;
        self.apply_action(
            order.action,
            quantity,
            fill_price,
            bar_number,
            date,
            time,
            &order.signal,
            3,
        )?;
        let filled = signed_abs(
            self.contracts.checked_sub(previous).ok_or_else(|| {
                KernelError::unsupported("contract delta overflow while recording fill")
            })?,
            "fill contract delta",
        )?;
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
        quantity: isize,
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

    #[allow(clippy::too_many_arguments)]
    fn apply_action(
        &mut self,
        action: Action,
        quantity: isize,
        fill_price: f64,
        bar_number: usize,
        date: i64,
        time: i64,
        signal: &str,
        category: i64,
    ) -> Result<(), KernelError> {
        match action {
            Action::Buy => {
                if self.market_position < 0 {
                    self.close_short(fill_price, bar_number, date, time, signal, category)?;
                }
                self.open_or_add_long(
                    quantity, fill_price, bar_number, date, time, signal, category,
                )?;
            }
            Action::SellShort => {
                if self.market_position > 0 {
                    self.close_long_fifo(
                        self.contracts,
                        fill_price,
                        bar_number,
                        date,
                        time,
                        signal,
                        category,
                    )?;
                }
                self.open_or_add_short(
                    quantity, fill_price, bar_number, date, time, signal, category,
                )?;
            }
            Action::Sell => {
                if self.market_position > 0 {
                    self.close_long_fifo(
                        quantity, fill_price, bar_number, date, time, signal, category,
                    )?;
                }
            }
            Action::BuyToCover => {
                if self.market_position < 0 {
                    self.close_short(fill_price, bar_number, date, time, signal, category)?;
                }
            }
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn open_or_add_long(
        &mut self,
        quantity: isize,
        fill_price: f64,
        bar_number: usize,
        date: i64,
        time: i64,
        signal: &str,
        category: i64,
    ) -> Result<(), KernelError> {
        let fill_price = narrow_f32(fill_price);
        let old_value = narrow_f32(self.entry_price * self.contracts as f64);
        self.market_position = 1;
        self.contracts = self.contracts.checked_add(quantity).ok_or_else(|| {
            KernelError::unsupported("long contract count overflow while adding an entry")
        })?;
        for _ in 0..quantity {
            self.entry_prices.push_back(fill_price);
        }
        self.entry_price = if self.contracts > 0 {
            narrow_f32((old_value + fill_price * quantity as f64) / self.contracts as f64)
        } else {
            narrow_f32(fill_price)
        };
        self.avg_entry_price = self.entry_price;

        if self.contracts == quantity {
            self.entry_bar = bar_number;
            self.position_profit = 0.0;
            self.reset_position_stats();
            self.entry_date = date;
            self.entry_time = time;
            self.entry_name = signal.to_owned();
            self.entry_category = category;
        } else {
            self.position_aggregates.entries = self
                .position_aggregates
                .entries
                .checked_add(1)
                .ok_or_else(|| KernelError::unsupported("position entry count overflow"))?;
        }
        self.position_aggregates.max_contracts =
            self.position_aggregates.max_contracts.max(self.contracts);
        self.bars_since_entry = 0;
        self.peak_profit = 0.0;
        self.max_contracts_held = self.max_contracts_held.max(self.contracts);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn open_or_add_short(
        &mut self,
        quantity: isize,
        fill_price: f64,
        bar_number: usize,
        date: i64,
        time: i64,
        signal: &str,
        category: i64,
    ) -> Result<(), KernelError> {
        let fill_price = narrow_f32(fill_price);
        let old_size = signed_abs(self.contracts, "short position size")?;
        let old_value = narrow_f32(self.entry_price * old_size as f64);
        self.market_position = -1;
        self.contracts = old_size.checked_add(quantity).ok_or_else(|| {
            KernelError::unsupported("short contract count overflow while adding an entry")
        })?;
        for _ in 0..quantity {
            self.entry_prices.push_back(fill_price);
        }
        if self.contracts == 0 {
            return Err(KernelError::unsupported(
                "short add produced zero contracts (negative quantity)",
            ));
        }
        self.entry_price =
            narrow_f32((old_value + fill_price * quantity as f64) / self.contracts as f64);
        self.avg_entry_price = self.entry_price;

        if self.contracts == quantity {
            self.entry_bar = bar_number;
            self.reset_position_stats();
            self.entry_date = date;
            self.entry_time = time;
            self.entry_name = signal.to_owned();
            self.entry_category = category;
        } else {
            self.position_aggregates.entries = self
                .position_aggregates
                .entries
                .checked_add(1)
                .ok_or_else(|| KernelError::unsupported("position entry count overflow"))?;
        }
        self.position_aggregates.max_contracts =
            self.position_aggregates.max_contracts.max(self.contracts);
        self.bars_since_entry = 0;
        self.position_profit = 0.0;
        self.peak_profit = 0.0;
        self.max_contracts_held = self.max_contracts_held.max(self.contracts);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn close_long_fifo(
        &mut self,
        quantity: isize,
        fill_price: f64,
        exit_bar: usize,
        exit_date: i64,
        exit_time: i64,
        exit_name: &str,
        exit_category: i64,
    ) -> Result<(), KernelError> {
        let closed = quantity.min(self.contracts);
        let pnl = (fill_price - self.entry_price) * self.instrument.point_value * closed as f64;
        self.record_realized_pnl(pnl)?;
        self.contracts = self.contracts.checked_sub(closed).ok_or_else(|| {
            KernelError::unsupported("long contract count overflow while closing")
        })?;
        for _ in 0..closed {
            self.entry_prices.pop_front();
        }
        if self.contracts > 0 && !self.entry_prices.is_empty() {
            let total: f64 = self.entry_prices.iter().sum();
            self.entry_price = narrow_f32(total / self.entry_prices.len() as f64);
            self.avg_entry_price = self.entry_price;
        } else if self.contracts <= 0 {
            self.record_closed_trade(
                bar_number_i64(exit_bar)?,
                fill_price,
                closed,
                true,
                exit_date,
                exit_time,
                exit_name,
                exit_category,
            );
            self.reset_flat();
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn close_short(
        &mut self,
        fill_price: f64,
        exit_bar: usize,
        exit_date: i64,
        exit_time: i64,
        exit_name: &str,
        exit_category: i64,
    ) -> Result<(), KernelError> {
        let fill_price = narrow_f32(fill_price);
        let previous = self.contracts;
        let pnl = (self.entry_price - fill_price) * self.instrument.point_value * previous as f64;
        self.record_realized_pnl(pnl)?;
        self.record_closed_trade(
            bar_number_i64(exit_bar)?,
            fill_price,
            previous,
            false,
            exit_date,
            exit_time,
            exit_name,
            exit_category,
        );
        self.reset_flat();
        Ok(())
    }

    fn record_realized_pnl(&mut self, pnl: f64) -> Result<(), KernelError> {
        self.net_profit += pnl;
        self.position_profit += pnl;
        self.total_trades = self
            .total_trades
            .checked_add(1)
            .ok_or_else(|| KernelError::unsupported("total trade count overflow"))?;
        if pnl > 0.0 {
            self.gross_profit += pnl;
        } else if pnl < 0.0 {
            self.gross_loss += pnl;
        }
        Ok(())
    }

    fn reset_position_stats(&mut self) {
        self.position_aggregates.entries = 1;
        self.position_aggregates.max_contracts = 0;
        self.position_aggregates.mfe_pc = 0.0;
        self.position_aggregates.mae_pc = 0.0;
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
        self.entry_date = 0;
        self.entry_time = 0;
        self.entry_name.clear();
        self.entry_category = 0;
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
        self.position_aggregates.mfe_pc = self.position_aggregates.mfe_pc.max(favorable);
        self.position_aggregates.mae_pc = self.position_aggregates.mae_pc.min(adverse);
    }

    fn update_drawdown(&mut self, bar: Bar, bar_number: usize) {
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
            self.max_id_drawdown = narrow_f32(-drawdown);
        }
    }

    fn check_money_management_stops(
        &mut self,
        bar: Bar,
        bar_number: usize,
    ) -> Result<(), KernelError> {
        if self.market_position == 0 || self.entry_bar == 0 {
            return Ok(());
        }

        let long = self.market_position > 0;
        let point_value = self.instrument.point_value;
        let entry = self.entry_price;
        let stop_loss = self.risk.stop_loss.filter(|amount| *amount > 0.0);
        let profit_target = self.risk.profit_target.filter(|amount| *amount > 0.0);
        if point_value == 0.0 && (stop_loss.is_some() || profit_target.is_some()) {
            return Err(KernelError::unsupported(
                "money-management level requires nonzero point value",
            ));
        }
        let stop_loss = match stop_loss {
            Some(amount) => Some(if long {
                self.snap(entry - amount / point_value, Snap::Down)?
            } else {
                self.snap(entry + amount / point_value, Snap::Up)?
            }),
            None => None,
        };
        let profit_target = match profit_target {
            Some(amount) => Some(if long {
                self.snap(entry + amount / point_value, Snap::Up)?
            } else {
                self.snap(entry - amount / point_value, Snap::Down)?
            }),
            None => None,
        };
        let breakeven = self.risk.breakeven.filter(|amount| *amount > 0.0);
        let trailing = self
            .risk
            .percent_trailing
            .filter(|(_, percent)| *percent > 0.0);

        let open = if bar.open > 0.0 { bar.open } else { bar.close };
        let high = if bar.high > 0.0 { bar.high } else { bar.close };
        let low = if bar.low > 0.0 { bar.low } else { bar.close };
        let mut peak = self.peak_profit;
        let favorable_price = if long { high } else { low };
        let point_value_contracts = point_value * self.contracts as f64;
        let best_pnl =
            (favorable_price - entry) * self.market_position as f64 * point_value_contracts;
        let epsilon = 1e-9;

        let open_levels = self.money_management_levels(
            peak,
            long,
            entry,
            point_value_contracts,
            stop_loss,
            profit_target,
            breakeven,
            trailing,
        )?;
        if open_levels.iter().any(|level| {
            (level.moves_up && open >= level.price - epsilon)
                || (!level.moves_up && open <= level.price + epsilon)
        }) {
            self.close_at_stop(open, bar_number, 0, 0, "", 0)?;
            return Ok(());
        }

        let nearer_low = (open - low).abs() <= (open - high).abs();
        let first = if nearer_low { low } else { high };
        let second = if nearer_low { high } else { low };
        let waypoints = [open, first, second, bar.close];
        for segment in waypoints.windows(2) {
            let start = segment[0];
            let end = segment[1];
            let moving_up = end >= start;
            let segment_low = start.min(end);
            let segment_high = start.max(end);
            let levels = self.money_management_levels(
                peak,
                long,
                entry,
                point_value_contracts,
                stop_loss,
                profit_target,
                breakeven,
                trailing,
            )?;
            let reachable: Vec<Level> = levels
                .into_iter()
                .filter(|level| {
                    level.moves_up == moving_up
                        && level.price >= segment_low - epsilon
                        && level.price <= segment_high + epsilon
                })
                .collect();
            if !reachable.is_empty() {
                let selected = select_extreme_index(&reachable, moving_up);
                self.close_at_stop(reachable[selected].price, bar_number, 0, 0, "", 0)?;
                return Ok(());
            }
            if (end - favorable_price).abs() < epsilon {
                peak =
                    peak.max((end - entry) * self.market_position as f64 * point_value_contracts);
            }
        }

        self.peak_profit = self.peak_profit.max(best_pnl);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn money_management_levels(
        &self,
        peak: f64,
        long: bool,
        entry: f64,
        point_value_contracts: f64,
        stop_loss: Option<f64>,
        profit_target: Option<f64>,
        breakeven: Option<f64>,
        trailing: Option<(f64, f64)>,
    ) -> Result<Vec<Level>, KernelError> {
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
        if breakeven.is_some_and(|amount| peak >= amount) {
            levels.push(Level {
                price: entry,
                moves_up: !long,
            });
        }
        if let Some((floor, percent)) = trailing {
            if peak >= floor {
                if point_value_contracts == 0.0 {
                    return Err(KernelError::unsupported(
                        "percent trailing requires nonzero point value times contracts",
                    ));
                }
                let giveback = peak * percent / 100.0;
                let offset = (peak - giveback) / point_value_contracts;
                levels.push(Level {
                    price: if long {
                        self.snap(entry + offset, Snap::Down)?
                    } else {
                        self.snap(entry - offset, Snap::Up)?
                    },
                    moves_up: !long,
                });
            }
        }
        Ok(levels)
    }

    #[allow(clippy::too_many_arguments)]
    fn close_at_stop(
        &mut self,
        price: f64,
        exit_bar: usize,
        exit_date: i64,
        exit_time: i64,
        exit_name: &str,
        exit_category: i64,
    ) -> Result<(), KernelError> {
        if self.market_position == 0 {
            return Ok(());
        }
        let is_long = self.market_position > 0;
        let size = self.contracts;
        let pnl = if is_long {
            (price - self.entry_price) * self.instrument.point_value * self.contracts as f64
        } else {
            (self.entry_price - price) * self.instrument.point_value * self.contracts as f64
        };
        self.record_realized_pnl(pnl)?;
        self.record_closed_trade(
            bar_number_i64(exit_bar)?,
            price,
            size,
            is_long,
            exit_date,
            exit_time,
            exit_name,
            exit_category,
        );
        self.reset_flat();
        Ok(())
    }

    fn snap(&self, price: f64, direction: Snap) -> Result<f64, KernelError> {
        if self.instrument.tick == 0.0 {
            return Ok(price);
        }
        let quotient = price / self.instrument.tick;
        let adjusted = match direction {
            Snap::Down => quotient + 1e-9,
            Snap::Up => quotient - 1e-9,
        };
        if !adjusted.is_finite() {
            return Err(KernelError::unsupported(format!(
                "non-finite money-management tick quotient {adjusted}"
            )));
        }
        let ticks = match direction {
            Snap::Down => adjusted.floor(),
            Snap::Up => adjusted.ceil(),
        };
        Ok(narrow_f32(ticks * self.instrument.tick))
    }
}

impl SessionEngine for FillEngine {
    fn before_bar(&mut self, bar: Bar, bar_number: usize) -> Result<(), KernelError> {
        self.before_bar_impl(bar, bar_number)
    }

    fn after_bar(
        &mut self,
        output: StrategyOutput,
        bar: Bar,
        bar_number: usize,
    ) -> Result<(), KernelError> {
        self.after_bar_impl(output, bar, bar_number)
    }

    fn position(&self) -> PositionView {
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

    fn fills(&self) -> &[Fill] {
        &self.fills
    }

    fn closed_positions(&self) -> &[ClosedPosition] {
        &self.closed_positions
    }

    fn position_aggregates(&self) -> &PositionAggregates {
        &self.position_aggregates
    }

    fn session_stats(&self) -> &SessionStats {
        &self.session_stats
    }

    #[allow(clippy::too_many_arguments)]
    fn record_closed_trade(
        &mut self,
        exit_bar: i64,
        exit_price: f64,
        size: isize,
        islong: bool,
        exit_date: i64,
        exit_time: i64,
        exit_name: &str,
        exit_cat: i64,
    ) {
        let entry_bar =
            i64::try_from(self.entry_bar).expect("validated entry bar must remain in i64 range");
        let bars = if entry_bar > 0 {
            exit_bar
                .checked_sub(entry_bar)
                .and_then(|difference| difference.checked_add(1))
                .expect("closed-position bar span overflow")
        } else {
            0
        };
        let mut mfe_pc = self.position_aggregates.mfe_pc;
        let mut mae_pc = self.position_aggregates.mae_pc;
        if exit_price != 0.0 && self.entry_price != 0.0 {
            let excursion = if islong {
                (narrow_f32(exit_price) - self.entry_price) * self.instrument.point_value
            } else {
                (self.entry_price - narrow_f32(exit_price)) * self.instrument.point_value
            };
            mfe_pc = mfe_pc.max(excursion);
            mae_pc = mae_pc.min(excursion);
        }

        self.closed_positions.push(ClosedPosition {
            pnl: self.position_profit,
            bars,
            mfe_pc,
            mae_pc,
            contracts: self.position_aggregates.max_contracts,
            entries: self.position_aggregates.entries,
            size,
            islong,
            entry_px: self.entry_price,
            exit_px: narrow_f32(exit_price),
            entry_bar,
            exit_bar,
            entry_date: self.entry_date,
            entry_time: self.entry_time,
            exit_date,
            exit_time,
            entry_name: self.entry_name.clone(),
            exit_name: exit_name.to_owned(),
            entry_cat: self.entry_category,
            exit_cat,
        });

        let stats = &mut self.session_stats;
        if self.position_profit > 0.0 {
            stats.n_win = stats
                .n_win
                .checked_add(1)
                .expect("winning-trade count overflow");
            stats.largest_win = stats.largest_win.max(self.position_profit);
            stats.total_bars_win = stats
                .total_bars_win
                .checked_add(bars)
                .expect("winning-trade bar total overflow");
            stats.cur_win_run = stats
                .cur_win_run
                .checked_add(1)
                .expect("winning-trade run overflow");
            stats.cur_loss_run = 0;
            stats.max_win_run = stats.max_win_run.max(stats.cur_win_run);
        } else if self.position_profit < 0.0 {
            stats.n_loss = stats
                .n_loss
                .checked_add(1)
                .expect("losing-trade count overflow");
            stats.largest_loss = stats.largest_loss.min(self.position_profit);
            stats.total_bars_loss = stats
                .total_bars_loss
                .checked_add(bars)
                .expect("losing-trade bar total overflow");
            stats.cur_loss_run = stats
                .cur_loss_run
                .checked_add(1)
                .expect("losing-trade run overflow");
            stats.cur_win_run = 0;
            stats.max_loss_run = stats.max_loss_run.max(stats.cur_loss_run);
        } else {
            stats.n_even = stats
                .n_even
                .checked_add(1)
                .expect("even-trade count overflow");
            stats.total_bars_even = stats
                .total_bars_even
                .checked_add(bars)
                .expect("even-trade bar total overflow");
            stats.cur_win_run = 0;
            stats.cur_loss_run = 0;
        }
    }
}

#[derive(Debug, Clone, Copy)]
enum QuantityMode {
    NextBar,
    OnClose,
}

fn resolve_quantity(
    raw_quantity: f64,
    mode: QuantityMode,
    exit_all_quantity: isize,
) -> Result<isize, KernelError> {
    if raw_quantity == 0.0 {
        return Ok(match mode {
            QuantityMode::NextBar => exit_all_quantity,
            QuantityMode::OnClose => 1,
        });
    }
    if !raw_quantity.is_finite() {
        return Err(KernelError::unsupported(format!(
            "non-finite order quantity {raw_quantity}"
        )));
    }
    let truncated = raw_quantity.trunc();
    let upper_exclusive = -(isize::MIN as f64);
    if truncated < isize::MIN as f64 || truncated >= upper_exclusive {
        return Err(KernelError::unsupported(format!(
            "order quantity {raw_quantity} is outside signed position range"
        )));
    }
    Ok(truncated as isize)
}

fn signed_abs(value: isize, context: &str) -> Result<isize, KernelError> {
    value.checked_abs().ok_or_else(|| {
        KernelError::unsupported(format!("{context} cannot represent its signed magnitude"))
    })
}

fn bar_number_i64(bar_number: usize) -> Result<i64, KernelError> {
    i64::try_from(bar_number)
        .map_err(|_| KernelError::input(format!("bar number {bar_number} exceeds i64 range")))
}

// Python's two-argument min/max keep the first value when comparison is false.
// That order matters for NaN and for the sign of equal zeroes.
fn python_min(first: f64, second: f64) -> f64 {
    if second < first {
        second
    } else {
        first
    }
}

fn python_max(first: f64, second: f64) -> f64 {
    if second > first {
        second
    } else {
        first
    }
}

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
