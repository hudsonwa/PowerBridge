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

//! Stable Rust-side types for the native accel kernel (interface v2).
//!
//! This module owns the *types and boundaries* the downstream modules build
//! against: the bar/series shapes, the order/fill/position surface, the
//! output/report surface, the interpreter seam, and the explicit instrument
//! config. Nothing here executes a strategy — the fill engine (`kernel`) and
//! the IR interpreter (`strategy`) are separate modules that consume these
//! types.
//!
//! Contract rules (see `docs/dev/ACCEL-INTERFACE-V2.md`):
//!
//! * **bit-exact or refuse** — every f32 narrowing point is explicit
//!   ([`narrow_f32`]); a missing or extra narrowing site is a silent trade
//!   divergence and a contract violation (interface v2).
//! * **fail loud** — any construct the kernel does not implement must raise;
//!   there is no partial-credit mode and no fallback inside the kernel.
//! * **deterministic** — no clock, timezone, locale, filesystem or RNG.
//! * **config is explicit** — nothing the Python path reads from outside the
//!   bars may be defaulted inside Rust; a missing field must raise, never be
//!   invented (interface v2). [`Config`] therefore has no `Default`.
//! * **signed sizes** — Python-reachable negative and zero contract sizes are
//!   `isize`, never unsigned.

use std::error::Error;
use std::fmt;

/// Narrow once to IEEE-754 binary32, then widen back to binary64.
///
/// INVARIANT — this is the ONLY place kernel arithmetic narrows to f32, and it
/// is called only where the Python engine calls `f32()` (`ctypes.c_float(x).value`).
/// Keeping kernel values as `f64` and narrowing at explicit sites avoids Rust
/// `f32`'s implicit round-after-every-op semantics. Every Python `f32()` site has a
/// matching `narrow_f32` site (17 Python sites vs 16 Rust: 3 sit in the
/// `_record_closed_trade` ledger, which the kernel omits, and 2 Rust sites are
/// unrolled match arms of Python's two conditional-price lines). `narrow_f32`
/// is bit-identical to `ctypes.c_float(x).value`, including at extreme values.
#[inline]
pub(crate) fn narrow_f32(value: f64) -> f64 {
    (value as f32) as f64
}

/// The crate-wide error type for the native kernel.
///
/// INVARIANT — every failure is loud and classified: `input` = malformed
/// caller input (e.g. a column-length mismatch), `unsupported` = a construct
/// outside the implemented surface (must be refused, never approximated),
/// `strategy` = the strategy/interpreter raised. Constructors are `pub(crate)`
/// so every module builds the same error type instead of inventing a parallel one.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct KernelError {
    message: String,
}

impl KernelError {
    pub(crate) fn input(message: impl Into<String>) -> Self {
        Self {
            message: format!("invalid accel input: {}", message.into()),
        }
    }

    pub(crate) fn unsupported(message: impl Into<String>) -> Self {
        Self {
            message: format!("unsupported native construct: {}", message.into()),
        }
    }

    pub(crate) fn strategy(message: impl Into<String>) -> Self {
        Self {
            message: format!("native strategy failed: {}", message.into()),
        }
    }
}

impl fmt::Display for KernelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl Error for KernelError {}

/// One bar of the primary series.
///
/// INVARIANT — `open`/`high`/`low`/`close`/`volume` are `f64` because they
/// arrive as Python floats and are stored un-narrowed in the tape; f32
/// narrowing happens only at the explicit [`narrow_f32`] sites (fill prices,
/// weighted entry, tick snap). `date`/`time` are the EasyLanguage integer
/// forms (YYMMDD / HHMM) that the FFI and fill records carry verbatim.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Bar {
    pub(crate) open: f64,
    pub(crate) high: f64,
    pub(crate) low: f64,
    pub(crate) close: f64,
    /// INVARIANT — `volume` is carried through the tape unchanged for the
    /// engine; no current fill logic reads it. Kept because the FFI
    /// `series` bundle includes it.
    pub(crate) volume: f64,
    pub(crate) date: i64,
    pub(crate) time: i64,
}

/// Columnar bar series — the `series` bundle of contract v2, length-validated.
///
/// INVARIANT — all seven columns are exactly equal length, enforced at
/// construction ([`BarTape::from_columns`] fails loud on any mismatch). This is
/// the shared shape the engine, the strategy/interpreter and the FFI layer all
/// name; the columns stay private so the length invariant cannot be broken
/// after construction. Per-bar access is [`BarTape::bar`].
#[derive(Debug, Clone)]
pub(crate) struct BarTape {
    open: Vec<f64>,
    high: Vec<f64>,
    low: Vec<f64>,
    close: Vec<f64>,
    volume: Vec<f64>,
    date: Vec<i64>,
    time: Vec<i64>,
}

impl BarTape {
    pub(crate) fn from_columns(
        open: Vec<f64>,
        high: Vec<f64>,
        low: Vec<f64>,
        close: Vec<f64>,
        volume: Vec<f64>,
        date: Vec<i64>,
        time: Vec<i64>,
    ) -> Result<Self, KernelError> {
        let expected = open.len();
        let lengths = [
            ("high", high.len()),
            ("low", low.len()),
            ("close", close.len()),
            ("volume", volume.len()),
            ("date", date.len()),
            ("time", time.len()),
        ];
        for (name, actual) in lengths {
            if actual != expected {
                return Err(KernelError::input(format!(
                    "column length mismatch: open has {expected} bars but {name} has {actual}"
                )));
            }
        }
        Ok(Self {
            open,
            high,
            low,
            close,
            volume,
            date,
            time,
        })
    }

    pub(crate) fn len(&self) -> usize {
        self.open.len()
    }

    pub(crate) fn bar(&self, index: usize) -> Bar {
        Bar {
            open: self.open[index],
            high: self.high[index],
            low: self.low[index],
            close: self.close[index],
            volume: self.volume[index],
            date: self.date[index],
            time: self.time[index],
        }
    }

    /// Columnar EL dates (YYYMMDD-offset). Used by skip-region proofs.
    pub(crate) fn dates(&self) -> &[i64] {
        &self.date
    }

    /// Columnar EL times (HHmm). Paired with [`dates`] for Data2 alignment.
    pub(crate) fn times(&self) -> &[i64] {
        &self.time
    }

    pub(crate) fn opens(&self) -> &[f64] {
        &self.open
    }

    pub(crate) fn highs(&self) -> &[f64] {
        &self.high
    }

    pub(crate) fn lows(&self) -> &[f64] {
        &self.low
    }

    pub(crate) fn closes(&self) -> &[f64] {
        &self.close
    }

    pub(crate) fn volumes(&self) -> &[f64] {
        &self.volume
    }
}

/// Order direction (the `Action` enum of the Python `FillEngine`).
///
/// INVARIANT — every fill type recorded is derived from this enum via
/// [`fill_side_type`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Action {
    Buy,
    SellShort,
    Sell,
    BuyToCover,
}

impl Action {
    pub(crate) fn is_buy_side(self) -> bool {
        matches!(self, Self::Buy | Self::BuyToCover)
    }
}

/// INVARIANT — the emitted fill-type strings are the current Python
/// `FillEngine.fills` convention (`pl_oracle` hashes these exact strings):
/// every buy-side fill records as `"EntryLong"`, every sell-side fill (an
/// explicit order or a risk exit) as `"ExitLong"`.
pub(crate) fn fill_side_type(buy_side: bool) -> &'static str {
    if buy_side {
        "EntryLong"
    } else {
        "ExitLong"
    }
}

/// Order execution style.
///
/// INVARIANT — `Close` is the this-bar-on-close queuing style; `Market` /
/// `Limit` / `Stop` are next-bar styles.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OrderType {
    Market,
    Limit,
    Stop,
    Close,
}

/// Bar-relative execution timing (Python `bar_timing`: 'this' / 'next').
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Timing {
    NextBar,
    ThisBar,
}

/// A single strategy order.
///
/// INVARIANT — `quantity` is a SIGNED `f64` that preserves Python
/// `FillEngine._fill_at` semantics exactly. A codegen `qty_expr`
/// is an arbitrary EL numeric expression (e.g. `Buy (Risk/StopDistance)
/// contracts`), so:
///
/// * `raw_qty == 0.0` is the "exit all" convention (`contracts.max(1)`);
/// * otherwise `int(raw_qty)` truncates toward zero and STAYS SIGNED (0.4 →
///   0 contracts / 0 fills phantom position; 2.7 → 2; -3 → contracts -3 with
///   3 fill records);
/// * a non-finite quantity (`NaN`/`inf`) must FAIL LOUD at fill time, exactly
///   as Python's `int()` raises.
///
/// `quantity` MUST NOT be narrowed to an unsigned integer anywhere on the path
/// to fill resolution — `usize` erased 0.4 into an "exit all" and minted a
/// phantom contract (a past regression). `price` is `None` exactly when Python
/// silently drops the order (a non-numeric index-5 slot); it is never
/// guessed.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Order {
    pub(crate) action: Action,
    pub(crate) order_type: OrderType,
    pub(crate) quantity: f64,
    pub(crate) timing: Timing,
    pub(crate) signal: String,
    pub(crate) price: Option<f64>,
    /// The `from entry("label")` of a named exit, else `None`. Python closes
    /// the exit's side as usual but fails loud when more than one entry is
    /// open on that side (`engine.py:_check_named_exit`); the kernel refuses
    /// the run there instead of guessing per-entry fills.
    pub(crate) from_entry: Option<String>,
}

impl Order {
    #[cfg(test)]
    pub(crate) fn market(action: Action, quantity: f64, signal: &str) -> Self {
        Self {
            action,
            order_type: OrderType::Market,
            quantity,
            timing: Timing::NextBar,
            signal: signal.to_owned(),
            price: None,
            from_entry: None,
        }
    }

    #[cfg(test)]
    pub(crate) fn conditional(
        action: Action,
        order_type: OrderType,
        quantity: f64,
        signal: &str,
        price: f64,
    ) -> Self {
        Self {
            action,
            order_type,
            quantity,
            timing: Timing::NextBar,
            signal: signal.to_owned(),
            price: Some(price),
            from_entry: None,
        }
    }
}

/// One fill record in the `FillEngine.fills` shape `pl_oracle` hashes.
///
/// INVARIANT — every record represents exactly ONE contract: `contracts` is
/// always `1` (Python appends one record per contract, `engine.py:304/394`).
/// Records follow the SIGNED position: a reversal emits its close-leg records
/// then its open-leg records, all typed by the order side, and risk exits
/// (stop loss, profit target, trailing, end of day) are recorded too.
/// `fill_type` is [`fill_side_type`]
/// (`"EntryLong"` / `"ExitLong"`). Signed sizing applies to position
/// `contracts`/`max_contracts_held` ([`PositionView`]), not to this per-record
/// count.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Fill {
    pub(crate) fill_type: &'static str,
    pub(crate) signal: String,
    pub(crate) date: i64,
    pub(crate) time: i64,
    pub(crate) price: f64,
    pub(crate) contracts: usize,
}

/// Per-bar position/trade observation the strategy sees and the report
/// columns read.
///
/// INVARIANT — `contracts` and `max_contracts_held` are SIGNED (`isize`)
/// because negative and zero sizes are reachable from Python (qty `-3` →
/// `contracts = -3`; fractional `0.4` → phantom `contracts = 0`). They must
/// never be narrowed to an unsigned type. The remaining fields mirror the
/// Python `FillEngine` position state at the same observation point;
/// `exit_price`/`bars_since_exit` are 0 in the current engine (Python never
/// sets them in `_reset_flat` either).
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct PositionView {
    pub(crate) market_position: i8,
    pub(crate) contracts: isize,
    pub(crate) entry_price: f64,
    pub(crate) avg_entry_price: f64,
    pub(crate) bars_since_entry: usize,
    pub(crate) exit_price: f64,
    pub(crate) bars_since_exit: usize,
    pub(crate) open_position_profit: f64,
    pub(crate) position_profit: f64,
    pub(crate) net_profit: f64,
    pub(crate) gross_profit: f64,
    pub(crate) gross_loss: f64,
    pub(crate) total_trades: usize,
    pub(crate) max_contracts_held: isize,
    pub(crate) max_id_drawdown: f64,
}

/// Strategy -> engine risk update for one bar.
///
/// INVARIANT — every field is optional per bar; `Some(v)` sets/overrides that
/// risk level, `None` leaves it unchanged. `percent_trailing` is `(floor,
/// percent)` mirroring Python's percent-trailing pair; `dollar_trailing` is the
/// SetDollarTrailing giveback amount. `exit_on_close` arms
/// the session-rollover flatten. The `*_pt` fields carry the MultiCharts
/// point-based variants (`SetStopLoss_pt` & co.); [`RiskUpdate::resolve_points`]
/// folds them into the currency fields before the engine applies the update.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct RiskUpdate {
    pub(crate) stop_loss: Option<f64>,
    pub(crate) profit_target: Option<f64>,
    pub(crate) breakeven: Option<f64>,
    pub(crate) percent_trailing: Option<(f64, f64)>,
    pub(crate) dollar_trailing: Option<f64>,
    pub(crate) exit_on_close: Option<bool>,
    pub(crate) stop_loss_pt: Option<f64>,
    pub(crate) profit_target_pt: Option<f64>,
    pub(crate) breakeven_pt: Option<f64>,
    pub(crate) percent_trailing_pt: Option<(f64, f64)>,
    pub(crate) trailing_stop_pt: Option<f64>,
}

impl RiskUpdate {
    /// Convert the point-based fields to currency (`points * point_value`),
    /// mirroring Python `FillEngine._resolve_point_risk`. A point amount set on
    /// the same bar as its currency form wins, as in the Python dict fold.
    pub(crate) fn resolve_points(mut self, point_value: f64) -> Self {
        if let Some(pts) = self.stop_loss_pt.take() {
            self.stop_loss = Some(pts * point_value);
        }
        if let Some(pts) = self.profit_target_pt.take() {
            self.profit_target = Some(pts * point_value);
        }
        if let Some(pts) = self.breakeven_pt.take() {
            self.breakeven = Some(pts * point_value);
        }
        if let Some((pts, pct)) = self.percent_trailing_pt.take() {
            self.percent_trailing = Some((pts * point_value, pct));
        }
        if let Some(pts) = self.trailing_stop_pt.take() {
            self.dollar_trailing = Some(pts * point_value);
        }
        self
    }
}

/// One bar's strategy output: explicit orders + risk update.
///
/// INVARIANT — this is the complete strategy-to-engine boundary surface
/// ([`NativeStrategy::on_bar`] returns it). The engine applies `orders`
/// (this-bar-on-close queued, everything else next-bar) and folds `risk` into
/// its money-management levels.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct StrategyOutput {
    pub(crate) orders: Vec<Order>,
    pub(crate) risk: RiskUpdate,
}

/// The per-run output surface (contract v2 `run_program` return).
///
/// INVARIANT — `fills` is the ordered trade list in the `FillEngine.fills`
/// shape `pl_oracle` hashes. `position` is the final per-bar observation, and
/// `total_trades` / `max_id_drawdown` are the report columns the caller
/// consumes. The per-bar trace is `strategy::trace::TypedTrace`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct KernelOutcome {
    pub(crate) fills: Vec<Fill>,
    pub(crate) position: PositionView,
    pub(crate) total_trades: usize,
    pub(crate) max_id_drawdown: f64,
    /// Bars the kernel did not evaluate because a skip-region proof showed they
    /// cannot emit an order. Zero when the skip was unprovable (full eval).
    pub(crate) skipped_bars: usize,
    /// Bars the engine short-circuited through the flat-bar no-work fast path.
    pub(crate) fast_path_hits: usize,
}

/// One trading-session boundary, in EL HHmm (NQ: 0830 -> 1515).
///
/// INVARIANT — session bounds are EXPLICIT config, never derived or guessed
/// natively (interface v2: a program that reads session words must run
/// against the right session, or be refused — never silently against the
/// wrong one). The kernel owns the session logic (`SetExitOnClose`
/// rollover, `SessionLastBar`) built on these bounds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Session {
    pub(crate) start: i64,
    pub(crate) end: i64,
}

/// One closed position as the positions-ago keywords (`EntryDate(n)`,
/// `ExitPrice(n)`, `MaxShares(n)`, ...) read it: the fields of a Python
/// `FillEngine.closed_trades` entry that `pl_pos_ago` serves. `exit_price` is
/// already narrowed to f32 and `contracts` is the most contracts held while
/// the position was open.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct ClosedTrade {
    pub(crate) entry_date: i64,
    pub(crate) entry_time: i64,
    pub(crate) entry_price: f64,
    pub(crate) exit_date: i64,
    pub(crate) exit_time: i64,
    pub(crate) exit_price: f64,
    pub(crate) contracts: isize,
}

/// The engine's trade history as the strategy sees it before a bar: the open
/// position's entry date/time and `MaxShares`, and every closed position so
/// far, oldest first.
#[derive(Debug, Clone, Copy)]
pub(crate) struct TradeHistory<'a> {
    pub(crate) entry_date: i64,
    pub(crate) entry_time: i64,
    pub(crate) max_shares: isize,
    pub(crate) closed: &'a [ClosedTrade],
}

/// The strategy/interpreter seam the `accel/strategy/` module implements and the
/// bar loop drives.
///
/// INVARIANT — a pure Rust seam: no Python callbacks and no per-bar FFI
/// crossing (that would destroy the one-call-per-run gain). The interpreter
/// over the v2 IR implements this to emit [`StrategyOutput`] per bar; a
/// generated Rust strategy can also implement it directly. It is deliberately
/// crate-private and cannot be backed by a Python callable.
pub(crate) trait NativeStrategy {
    fn on_bar(
        &mut self,
        bar: Bar,
        bar_number: usize,
        position: PositionView,
    ) -> Result<StrategyOutput, KernelError>;

    /// The engine's trade history, given before each [`NativeStrategy::on_bar`].
    /// The default ignores it; the IR interpreter keeps what the positions-ago
    /// keywords read.
    fn observe_trade_history(&mut self, _history: TradeHistory<'_>) {}

    /// Prove a prefix of `tape` cannot emit an order, or refuse loudly.
    ///
    /// The default is "unproven" — full evaluation. The IR interpreter
    /// overrides this with a date-window / session-warmup proof. A skip that
    /// would have traded is a catastrophic bug; when the proof cannot be
    /// discharged the kernel evaluates every bar.
    fn skip_plan(&self, _tape: &BarTape) -> SkipPlan {
        SkipPlan::unproven("strategy does not implement a skip proof")
    }
}

/// Result of a skip-region proof over a tape.
///
/// `proven` is false when the kernel must evaluate every bar. `eval_from` is
/// the first tape index that still runs the strategy (warmup sessions included).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SkipPlan {
    pub(crate) eval_from: usize,
    pub(crate) proven: bool,
    pub(crate) reason: String,
}

impl SkipPlan {
    pub(crate) fn unproven(reason: impl Into<String>) -> Self {
        Self {
            eval_from: 0,
            proven: false,
            reason: reason.into(),
        }
    }

    pub(crate) fn skip(eval_from: usize, reason: impl Into<String>) -> Self {
        Self {
            eval_from,
            proven: true,
            reason: reason.into(),
        }
    }
}

impl<F> NativeStrategy for F
where
    F: FnMut(Bar, usize, PositionView) -> Result<StrategyOutput, KernelError>,
{
    fn on_bar(
        &mut self,
        bar: Bar,
        bar_number: usize,
        position: PositionView,
    ) -> Result<StrategyOutput, KernelError> {
        self(bar, bar_number, position)
    }
}

/// The engine's PnL/snap subset, derived from [`Config`].
///
/// INVARIANT — `point_value` is `$` per FULL POINT per contract
/// (`Config::bigpointvalue`, NQ 20) — the value PnL multiplies
/// `(fill - entry) * point_value * contracts` by; `tick` is the snap grid
/// (NQ 0.25). This is the engine-consumed slice of the explicit [`Config`] and
/// is NOT the caller-facing config: v2 requires every config field to be
/// supplied by the caller ([`Config`] has no `Default`). `Default` here exists
/// only as a test fixture (NQ 20 / 0.25) and must not be used on a
/// caller-supplied path.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Instrument {
    pub(crate) point_value: f64,
    pub(crate) tick: f64,
    /// Same-direction entry cap (`Config::max_entries_per_direction`); 0 is
    /// unlimited.
    pub(crate) max_entries_per_direction: usize,
}

impl Default for Instrument {
    fn default() -> Self {
        Self {
            point_value: 20.0,
            tick: 0.25,
            max_entries_per_direction: 0,
        }
    }
}

impl From<&Config> for Instrument {
    fn from(config: &Config) -> Self {
        Self {
            point_value: config.bigpointvalue,
            tick: config.tick,
            max_entries_per_direction: config.max_entries_per_direction,
        }
    }
}

/// Explicit per-run instrument/study config (contract v2 `config` argument).
///
/// INVARIANT — EVERY field is supplied by the caller and there is NO `Default`:
/// nothing the Python path reads from outside the bars may be defaulted inside
/// Rust, and a missing field must raise at the boundary, never be invented
/// (interface v2). Field names and values mirror the Python
/// `InstrumentConfig` (NQ: `bigpointvalue` 20, `tick` 0.25, `sess1starttime`
/// 830) plus the v2-required commission, slippage, session bounds, MaxBarsBack
/// and warmup seed. [`From<&Config> for Instrument`] pins the narrowing-free
/// mapping the engine consumes (`point_value = bigpointvalue`).
///
/// Built from the FFI `config` dict by `extract_config` in `lib.rs`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Config {
    pub(crate) name: String,
    pub(crate) bigpointvalue: f64,
    pub(crate) pointvalue: f64,
    pub(crate) minmove: i64,
    pub(crate) pricescale: i64,
    pub(crate) tick: f64,
    pub(crate) barinterval: i64,
    pub(crate) session: Session,
    pub(crate) commission: f64,
    pub(crate) slippage: f64,
    pub(crate) max_bars_back: usize,
    /// 0 = unlimited same-direction entries; N >= 1 refuses a Buy/SellShort
    /// while N entries are open in that direction (reversals still fill).
    pub(crate) max_entries_per_direction: usize,
    pub(crate) warmup_seed: usize,
    pub(crate) symccy: i64,
    pub(crate) symnm: String,
    pub(crate) symalt: String,
    pub(crate) descstr: String,
    pub(crate) expdate: i64,
    /// $ per contract subtracted from each closed trade's gross profit in the
    /// audit packet (commission plus slippage for one round turn). Fills and
    /// the stable `run_program` path do not read it.
    pub(crate) round_turn_cost: f64,
}

// ============================================================================
// END OF SHARED TYPES. Everything above is the interface both implementation
// modules build against:
//
//   KERNEL   (accel/src/kernel.rs)   — fill engine, bar loop, session state
//   STRATEGY (accel/src/strategy/**) — the generated-strategy interpreter
//
// We keep these types in one place so the kernel and the interpreter cannot
// drift into two incompatible views of the same order/fill/trace surface;
// widening them is a deliberate interface change, not a local edit.
// ============================================================================

// ===== ENGINE-LANE REGION — append below this line only =====

// ===== STRATEGY-LANE REGION — append below this line only =====
