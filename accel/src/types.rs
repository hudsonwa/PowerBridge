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

//! Frozen Rust-side contract for the native accel kernel (interface v2).
//!
//! This module owns the *types and boundaries* the downstream lanes build
//! against: the bar/series shapes, the order/fill/position surface, the
//! closed-position ledger (verifier finding D4), the output/report surface,
//! the session/engine/interpreter seams, and the explicit instrument config.
//! Nothing here executes a strategy — the fill engine, the IR interpreter and
//! the harness are separate lanes that consume these types.
//!
//! Contract rules (see `docs/dev/ACCEL-INTERFACE-V2.md`):
//!
//! * **bit-exact or refuse** — every f32 narrowing point is explicit
//!   ([`narrow_f32`]); a missing or extra narrowing site is a silent trade
//!   divergence and a contract violation (v2 requirement 5).
//! * **fail loud** — any construct the kernel does not implement must raise;
//!   there is no partial-credit mode and no fallback inside the kernel.
//! * **deterministic** — no clock, timezone, locale, filesystem or RNG.
//! * **config is explicit** — nothing the Python path reads from outside the
//!   bars may be defaulted inside Rust; a missing field must raise, never be
//!   invented (v2 requirements 2-3). [`Config`] therefore has no `Default`.
//! * **signed sizes** — Python-reachable negative and zero contract sizes are
//!   `isize`, never unsigned (verifier D1).

use std::error::Error;
use std::fmt;

/// Narrow once to IEEE-754 binary32, then widen back to binary64.
///
/// INVARIANT — this is the ONLY place kernel arithmetic narrows to f32, and it
/// is called only where the Python engine calls `f32()` (`ctypes.c_float(x).value`).
/// Keeping kernel values as `f64` and narrowing at explicit sites avoids Rust
/// `f32`'s implicit round-after-every-op semantics. The verifier reconciled the
/// Python sites against the Rust sites exactly (17 Python `f32()` call sites
/// vs 16 Rust `narrow_f32` sites: the 3 missing are inside the omitted
/// `_record_closed_trade` ledger, and the 2 Rust-extra are unrolled match arms
/// of Python's two conditional-price lines). `narrow_f32` is bit-identical to
/// `ctypes.c_float(x).value` on the audited extreme values.
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
/// so every lane builds the same error type instead of inventing a parallel one.
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
    /// engine lane; no current fill logic reads it. Kept because the FFI
    /// `series` bundle includes it.
    #[allow(dead_code)]
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
/// [`Action::python_fill_type`]. Variants are constructed by the kernel engine,
/// which is dormant in production until an owner-approved strategy contract
/// lands (`#[allow(dead_code)] mod kernel` in `lib.rs`) and is exercised by the
/// unit tests; that is the only reason this enum carries `#[allow(dead_code)]`.
#[allow(dead_code)]
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

    /// INVARIANT — the emitted fill-type strings are the current Python
    /// `FillEngine.fills` convention (`pl_oracle` hashes these exact strings):
    /// every buy-side action records as `"EntryLong"`, every sell-side as
    /// `"ExitLong"`.
    pub(crate) fn python_fill_type(self) -> &'static str {
        if self.is_buy_side() {
            "EntryLong"
        } else {
            "ExitLong"
        }
    }
}

/// Order execution style.
///
/// INVARIANT — `Close` is the this-bar-on-close queuing style; `Market` /
/// `Limit` / `Stop` are next-bar styles. Variants are constructed by the
/// dormant kernel engine (see [`Action`] for why this carries
/// `#[allow(dead_code)]`).
#[allow(dead_code)]
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
/// `FillEngine._fill_at` semantics exactly (verifier D1). A codegen `qty_expr`
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
/// phantom contract (the D1 regression). `price` is `None` exactly when Python
/// silently drops the order (a non-numeric index-5 slot, D2); it is never
/// guessed.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Order {
    pub(crate) action: Action,
    pub(crate) order_type: OrderType,
    pub(crate) quantity: f64,
    pub(crate) timing: Timing,
    pub(crate) signal: String,
    pub(crate) price: Option<f64>,
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
        }
    }
}

/// One fill record in the `FillEngine.fills` shape `pl_oracle` hashes.
///
/// INVARIANT — every record represents exactly ONE contract: `contracts` is
/// always `1` (Python appends one record per contract, `engine.py:304/394`).
/// A mid-bar reversal of `n` contracts emits a single record for the full `n`
/// turnover, typed by the action — that quirk is shared with Python, which is
/// why `round_turns = len(fills) // 2` is a valid cross-check only because
/// both sides reproduce it. `fill_type` is [`Action::python_fill_type`]
/// (`"EntryLong"` / `"ExitLong"`). Signed sizing (D1) applies to position
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
/// never be narrowed to an unsigned type (D1). The remaining fields mirror the
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
/// percent)` mirroring Python's percent-trailing pair. `exit_on_close` arms
/// the session-rollover flatten. The `*_pt` fields carry the MultiCharts
/// point-based variants (`SetStopLoss_pt` & co.); [`RiskUpdate::resolve_points`]
/// folds them into the currency fields before the engine applies the update.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct RiskUpdate {
    pub(crate) stop_loss: Option<f64>,
    pub(crate) profit_target: Option<f64>,
    pub(crate) breakeven: Option<f64>,
    pub(crate) percent_trailing: Option<(f64, f64)>,
    pub(crate) exit_on_close: Option<bool>,
    pub(crate) stop_loss_pt: Option<f64>,
    pub(crate) profit_target_pt: Option<f64>,
    pub(crate) breakeven_pt: Option<f64>,
    pub(crate) percent_trailing_pt: Option<(f64, f64)>,
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

/// One closed-position snapshot — the `_record_closed_trade` ledger record.
///
/// INVARIANT — mirrors the Python `FillEngine._record_closed_trade` dict
/// (`engine.py:529-574`) field-for-field. It is the consumer of the engine's
/// currently write-only `position_mfe_per_contract` /
/// `position_mae_per_contract` excursion state (verifier D4): at close time
/// `mfe_pc`/`mae_pc` fold in the exit excursion at `f32(exit_price)`, because
/// the exit fill is part of the held period. `contracts` is the MAX `isize`
/// contracts held while open (`_pos_max_contracts`), `entries` the
/// separate-entry count (`_pos_entries`), and `size` the contracts closed at
/// the exit fill — a scale-in ladder is ONE closed position (counted here)
/// with many fill records (counted by `round_turns`), which is exactly why
/// `closed_positions` is NOT part of the trade digest. Entry context is
/// snapshotted BEFORE `_reset_flat`.
///
/// Not yet constructed by any code in this crate: the D4 engine lane populates
/// it via [`SessionEngine::record_closed_trade`].
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ClosedPosition {
    pub(crate) pnl: f64,
    pub(crate) bars: i64,
    pub(crate) mfe_pc: f64,
    pub(crate) mae_pc: f64,
    pub(crate) contracts: isize,
    pub(crate) entries: i64,
    pub(crate) size: isize,
    pub(crate) islong: bool,
    pub(crate) entry_px: f64,
    pub(crate) exit_px: f64,
    pub(crate) entry_bar: i64,
    pub(crate) exit_bar: i64,
    pub(crate) entry_date: i64,
    pub(crate) entry_time: i64,
    pub(crate) exit_date: i64,
    pub(crate) exit_time: i64,
    pub(crate) entry_name: String,
    pub(crate) exit_name: String,
    pub(crate) entry_cat: i64,
    pub(crate) exit_cat: i64,
}

/// The open position's running aggregates (`_pos_entries` / `_pos_max_contracts`)
/// plus its per-contract excursions (`_pos_mfe_pc` / `_pos_mae_pc`).
///
/// INVARIANT — `max_contracts` is the max contracts held while open
/// (non-negative by construction but `isize` per the signed D1 contract);
/// `entries` counts separate entry fills; `mfe_pc`/`mae_pc` are `$` per
/// contract with `mae_pc <= 0`. These are the aggregates the GTA6 report
/// columns read O(1) instead of rescanning the ledger.
///
/// Maintained by the D4 engine lane; not yet constructed by this crate.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PositionAggregates {
    pub(crate) entries: i64,
    pub(crate) max_contracts: isize,
    pub(crate) mfe_pc: f64,
    pub(crate) mae_pc: f64,
}

/// Incremental GTA6 session/strategy statistics (`_st_*`), updated at trade
/// close so the report columns are O(1) reads, never rescans of the ledger.
///
/// INVARIANT — every closed trade is exactly one of win (`pnl > 0`), loss
/// (`pnl < 0`) or even (`pnl == 0`), so
/// `n_win + n_loss + n_even == closed_positions.len()` and the running totals
/// reproduce a full ledger rescan byte-for-byte — the equivalence the Python
/// `_st_*` fields rely on (`engine.py:575-602`). `largest_win`/`largest_loss`
/// floor at 0.0 like Python.
///
/// Maintained by the D4 engine lane; not yet constructed by this crate.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct SessionStats {
    pub(crate) n_win: i64,           // _st_nwin      NumWinTrades
    pub(crate) n_loss: i64,          // _st_nlos      NumLosTrades
    pub(crate) n_even: i64,          // _st_neven     NumEvenTrades
    pub(crate) largest_win: f64,     // _st_lrgwin   LargestWinTrade
    pub(crate) largest_loss: f64,    // _st_lrglos   LargestLosTrade
    pub(crate) total_bars_win: i64,  // _st_totbwin   TotalBarsWinTrades
    pub(crate) total_bars_loss: i64, // _st_totblos   TotalBarsLosTrades
    pub(crate) total_bars_even: i64, // _st_totbeven  TotalBarsEvenTrades
    pub(crate) cur_win_run: i64,     // _st_cwin      current consecutive-winners run
    pub(crate) cur_loss_run: i64,    // _st_clos      current consecutive-losers run
    pub(crate) max_win_run: i64,     // _st_maxcwin   MaxConsecWinners
    pub(crate) max_loss_run: i64,    // _st_maxclos   MaxConsecLosers
}

/// The per-run output surface (contract v2 `run_program` return).
///
/// INVARIANT — `fills` is the ordered trade list in the `FillEngine.fills`
/// shape `pl_oracle` hashes; `closed_positions` is the D4 ledger whose LENGTH
/// is the `closed_positions` count `pl_oracle` reads
/// (`closed_positions = len(FillEngine.closed_trades)`) — a scale-in ladder is
/// one closed position, many fills. `position` is the final per-bar
/// observation, and `total_trades` / `max_id_drawdown` are the report columns
/// the caller consumes. The per-bar trace/report COLUMNS are the [`TraceReport`]
/// surface; `fills` here and `closed_positions` here are the two ledgers the
/// caller reads out.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct KernelOutcome {
    pub(crate) fills: Vec<Fill>,
    pub(crate) position: PositionView,
    pub(crate) total_trades: usize,
    pub(crate) max_id_drawdown: f64,
    pub(crate) closed_positions: Vec<ClosedPosition>,
    /// Bars the kernel did not evaluate because a skip-region proof showed they
    /// cannot emit an order. Zero when the skip was unprovable (full eval).
    pub(crate) skipped_bars: usize,
    /// Bars the engine short-circuited through the flat-bar no-work fast path.
    pub(crate) fast_path_hits: usize,
}

/// The per-bar trace/report surface the caller reads (`pl_run` CSV columns +
/// rows).
///
/// INVARIANT — mirrors `pl_run`/`run_el` output: `columns` are the first bar's
/// sorted trace keys; there is one `rows` entry per emitted bar with a cell
/// per column. Cell format is unchanged from the Python path: a
/// missing/`None` value renders as `'0'`, every other value as `str(value)`.
/// These columns are what the caller actually consumes — leaving them out of
/// the phase-1 freeze was the mistake the v2 refreeze exists to avoid.
///
/// Produced by the interpreter/engine lane; not yet constructed by this crate.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct TraceReport {
    pub(crate) columns: Vec<String>,
    pub(crate) rows: Vec<Vec<String>>,
}

/// One trading-session boundary, in EL HHmm (NQ: 0830 -> 1515).
///
/// INVARIANT — session bounds are EXPLICIT config, never derived or guessed
/// natively (v2 requirement 3: a program that reads session words must run
/// against the right session, or be refused — never silently against the
/// wrong one). The engine lane owns the session logic (`SetExitOnClose`
/// rollover, `SessionLastBar`) built on these bounds.
#[allow(dead_code)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Session {
    pub(crate) start: i64,
    pub(crate) end: i64,
}

/// The fill-engine boundary the `accel/engine/` lane implements and the bar
/// loop drives.
///
/// INVARIANT — this is the single engine contract both builder lanes share, so
/// they cannot invent a parallel fill-engine type: the per-bar lifecycle
/// (`before_bar` / `after_bar`), the strategy-observable [`PositionView`], the
/// ordered trade list, and the D4 closed-position ledger with its
/// `_record_closed_trade` entry point. `record_closed_trade` snapshots the
/// just-closed position BEFORE the flat reset, exactly like Python; the D4
/// lane wires the engine's close paths to call it and to maintain
/// [`PositionAggregates`] / [`SessionStats`].
///
/// Not implemented yet: the existing `kernel::FillEngine` predates D4 and is
/// the base the engine lane extends.
#[allow(dead_code)]
pub(crate) trait SessionEngine {
    fn before_bar(&mut self, bar: Bar, bar_number: usize) -> Result<(), KernelError>;
    fn after_bar(
        &mut self,
        output: StrategyOutput,
        bar: Bar,
        bar_number: usize,
    ) -> Result<(), KernelError>;
    fn position(&self) -> PositionView;
    fn fills(&self) -> &[Fill];
    fn closed_positions(&self) -> &[ClosedPosition];
    fn position_aggregates(&self) -> &PositionAggregates;
    fn session_stats(&self) -> &SessionStats;

    /// The `_record_closed_trade` entry point (D4): snapshot the just-closed
    /// position into the ledger. `exit_bar`/`exit_price`/`size`/`islong`/
    /// `exit_date`/`exit_time`/`exit_name`/`exit_cat` are the close-time
    /// context; the entry context is read from the engine's open-position
    /// state, which is still set at this point. The 8 parameters mirror the
    /// Python signature verbatim (`engine.py:529`), so the `too_many_arguments`
    /// lint is deliberately allowed rather than collapsing the contract.
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
    );
}

/// The strategy/interpreter seam the `accel/strategy/` lane implements and the
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
}

impl Default for Instrument {
    fn default() -> Self {
        Self {
            point_value: 20.0,
            tick: 0.25,
        }
    }
}

impl From<&Config> for Instrument {
    fn from(config: &Config) -> Self {
        Self {
            point_value: config.bigpointvalue,
            tick: config.tick,
        }
    }
}

/// Explicit per-run instrument/study config (contract v2 `config` argument).
///
/// INVARIANT — EVERY field is supplied by the caller and there is NO `Default`:
/// nothing the Python path reads from outside the bars may be defaulted inside
/// Rust, and a missing field must raise at the boundary, never be invented
/// (v2 requirements 2-3). Field names and values mirror the Python
/// `InstrumentConfig` (NQ: `bigpointvalue` 20, `tick` 0.25, `sess1starttime`
/// 830) plus the v2-required commission, slippage, session bounds, MaxBarsBack
/// and warmup seed. [`From<&Config> for Instrument`] pins the narrowing-free
/// mapping the engine consumes (`point_value = bigpointvalue`).
///
/// Consumed by the FFI lane; not yet constructed by this crate.
#[allow(dead_code)]
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
    pub(crate) warmup_seed: usize,
    pub(crate) symccy: i64,
    pub(crate) symnm: String,
    pub(crate) symalt: String,
    pub(crate) descstr: String,
    pub(crate) expdate: i64,
}

// ============================================================================
// FROZEN CONTRACT ENDS HERE. Nothing above this line may be edited by a builder
// lane. Two lanes work in parallel against this contract:
//
//   ENGINE LANE   (accel/src/engine/**)   — fill engine, bar loop, session state
//   STRATEGY LANE (accel/src/strategy/**) — the generated-strategy interpreter
//
// Each lane appends ONLY inside its own region below. Two edits to the SAME file
// merge cleanly when they land in DIFFERENT regions (verified) and conflict when
// both append at the same point — which is why the regions are separated here.
//
// If the frozen contract is genuinely wrong for what you need, STOP and say so in
// your report. Do NOT silently widen it: two lanes independently widening the same
// contract produce two half-contracts that each compile but disagree, and a clean
// git merge of divergent semantics is worse than a conflict, because nothing flags
// it.
//
// The trace/report surface the caller consumes is `TraceReport` (see above) — it is
// already in the freeze. Do not invent a second one.
// ============================================================================

// ===== ENGINE-LANE REGION — append below this line only =====

// ===== STRATEGY-LANE REGION — append below this line only =====
