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

//! Bar-by-bar execution of a compiled program (see `super::compile`).
//!
//! The v2 program IR is lowered once (in `Interpreter::new`) into a flat
//! `Vec<Instr>`, then specialised for the frozen Input vector:
//! constants are folded in, no-ops and dead `Once` bodies are dropped, and
//! common numeric sequences become a single opcode. Each `on_bar` walks the
//! specialised stream — first-bar and rest are separate blocks, so there is
//! no `JumpIfNotFirstBar` on the hot path. No Python and no per-bar FFI.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::Value as Json;

use crate::types::{
    narrow_f32, Bar, BarTape, ClosedTrade, KernelError, NativeStrategy, Order, PositionView,
    RiskUpdate, SkipPlan, StrategyOutput, TradeHistory,
};

use super::arrays::{py_index_int, ElArray};
use super::clock::{clock_value, python_clock, Clock};
use super::compile::{
    compile, Binop, CallOp, Cmp, ElemKind, Instr, McKw, PosAgoField, PositionKw, Program, RiskOp,
    Series, SeriesSrc, Unary,
};
use super::datefmt::format_date;
use super::drawing::{
    find_anchor, gradient_color, legacy_color_to_rgb, rgb, DrawingStore, TapeIndex,
};
use super::pynum::{
    binop_num, py_datetojulian, py_extreme, py_floor, py_int, py_round, py_sign, py_square,
};
use super::specialize::{specialize_with, Specialized, Stats as SpecializeStats};
use super::value::{num_to_str, pl_average, pl_crosses_above, pl_crosses_below, SeriesView, Value};

pub(crate) struct Interpreter<'a> {
    program: Program,
    /// Specialised stream for this variant's Inputs. Shared via
    /// `Arc` so `run` can borrow it without `mem::take` of the opcode vec.
    code: Arc<Specialized>,
    #[allow(dead_code)]
    specialize_stats: SpecializeStats,
    vars: Vec<Option<Value>>,
    var_numeric: Vec<bool>,
    var_double_accum: Vec<bool>,
    var_inits: Vec<Value>,
    stateful: Vec<usize>,
    inputs: Vec<Value>,
    needs_value_history: Vec<bool>,
    needs_numeric_history: Vec<bool>,
    history: Vec<Vec<Value>>,
    num_history: Vec<Vec<f64>>,
    prev: Vec<Value>,
    open: Vec<f64>,
    high: Vec<f64>,
    low: Vec<f64>,
    close: Vec<f64>,
    volume: Vec<f64>,
    date: Vec<i64>,
    time: Vec<i64>,
    data2: Option<Arc<Data2Aligned<'a>>>,
    bar: Bar,
    bar_number: usize,
    /// MaxBarsBack history-only bars fed before the first evaluated bar; the
    /// Data2 alignment index is the tape index, `history_bars + bar_number - 1`.
    history_bars: usize,
    position: PositionView,
    /// The engine's trade history as of this bar (see
    /// [`NativeStrategy::observe_trade_history`]).
    entry_date: i64,
    entry_time: i64,
    max_shares: isize,
    closed_trades: Vec<ClosedTrade>,
    first_bar: bool,
    orders: Vec<Order>,
    risk: RiskUpdate,
    stack: Vec<Value>,
    drawings: DrawingStore,
    /// The whole run tape, for drawing anchor `bn` (see
    /// [`Self::set_drawing_tape`]). Empty unless the drawings are dumped.
    drawing_tape: TapeIndex,
    /// Declared arrays. Allocated once and never reset between bars (the
    /// Python `_state` keeps the same list); a new interpreter starts fresh.
    arrays: Vec<ElArray>,
    /// `(current, end)` of each `for` loop.
    loops: Vec<(i64, i64)>,
    /// Type of the element the last `ArrayGet` read (for `ElemKind::Read`).
    last_read_kind: ElemKind,
    /// The run's `MinMove` and `PriceScale` (see [`Self::set_price_units`]).
    minmove: f64,
    pricescale: f64,
    /// Time source of the computer-clock words (see [`super::clock`]).
    clock: Clock,
}

/// Largest `for` bound the kernel runs. Python keeps the loop variable as an
/// `int` while the kernel reloads it through f32 on the next bar, which is
/// exact only up to 2^24.
const MAX_FOR_BOUND: i64 = 1 << 24;

impl<'a> Interpreter<'a> {
    pub(crate) fn new(
        ast: Json,
        input_overrides: &HashMap<String, Value>,
        data2: Option<Arc<Data2Aligned<'a>>>,
    ) -> Result<Self, KernelError> {
        let program = compile(&ast, input_overrides)?;
        Self::from_program(program, data2)
    }

    /// Build an interpreter from an already-compiled [`Program`].
    ///
    /// The sweep/batch path compiles once and clones the program per variant
    /// (swapping in the variant's resolved inputs) so it never re-walks the
    /// AST; this constructor is the seam it drives.
    pub(crate) fn from_program(
        program: Program,
        data2: Option<Arc<Data2Aligned<'a>>>,
    ) -> Result<Self, KernelError> {
        Self::from_program_with(program, data2, false)
    }

    /// [`Self::from_program`] for the trace entry point: `keep_stores` keeps
    /// stores to variables nothing reads, so the trace sees their values.
    pub(crate) fn from_program_with(
        program: Program,
        data2: Option<Arc<Data2Aligned<'a>>>,
        keep_stores: bool,
    ) -> Result<Self, KernelError> {
        let n_vars = program.var_names.len();
        let spec = specialize_with(&program, keep_stores);
        let specialize_stats = spec.stats;
        let arrays = program.arrays.iter().map(ElArray::new).collect();
        let loops = vec![(0, 0); program.n_loops];
        let mut interp = Self {
            var_numeric: program.var_numeric.clone(),
            var_double_accum: program.var_double_accum.clone(),
            var_inits: program.var_inits.clone(),
            stateful: program.stateful.clone(),
            inputs: program.inputs.clone(),
            needs_value_history: program.var_needs_value_history.clone(),
            needs_numeric_history: program.var_needs_numeric_history.clone(),
            code: Arc::new(spec),
            specialize_stats,
            program,
            vars: vec![None; n_vars],
            history: vec![Vec::new(); n_vars],
            num_history: vec![Vec::new(); n_vars],
            prev: vec![],
            // PAD=1 leading zero: mirrors `run_bar_loop` so Average / crosses
            // skip the same padding the Python path skips.
            open: vec![0.0],
            high: vec![0.0],
            low: vec![0.0],
            close: vec![0.0],
            volume: vec![0.0],
            date: vec![0],
            time: vec![0],
            data2,
            bar: Bar {
                open: 0.0,
                high: 0.0,
                low: 0.0,
                close: 0.0,
                volume: 0.0,
                date: 0,
                time: 0,
            },
            bar_number: 0,
            history_bars: 0,
            position: empty_position(),
            entry_date: 0,
            entry_time: 0,
            max_shares: 0,
            closed_trades: Vec::new(),
            first_bar: true,
            orders: Vec::new(),
            risk: RiskUpdate::default(),
            stack: Vec::new(),
            drawings: DrawingStore::new(),
            drawing_tape: TapeIndex::default(),
            arrays,
            loops,
            last_read_kind: ElemKind::Unknown,
            minmove: 0.0,
            pricescale: 0.0,
            clock: python_clock,
        };
        interp.seed_state();
        Ok(interp)
    }

    /// Specialisation cost and stream size (reported separately from the loop).
    #[allow(dead_code)]
    pub(crate) fn specialize_stats(&self) -> SpecializeStats {
        self.specialize_stats
    }

    fn seed_state(&mut self) {
        for slot in 0..self.var_inits.len() {
            let value = self.var_inits[slot].clone();
            self.prev.push(value.clone());
            if self.needs_value_history[slot] {
                self.history[slot] = vec![value.clone()];
            }
            if self.needs_numeric_history[slot] {
                if let Ok(num) = value.as_f64() {
                    self.num_history[slot] = vec![num];
                }
            }
        }
    }

    /// Append a MaxBarsBack bar to the price history without evaluating the
    /// program: no orders, no variable updates, and `BarNumber` does not advance.
    pub(crate) fn observe_history(&mut self, bar: Bar) {
        self.open.push(bar.open);
        self.high.push(bar.high);
        self.low.push(bar.low);
        self.close.push(bar.close);
        self.volume.push(bar.volume);
        self.date.push(bar.date);
        self.time.push(bar.time);
        self.history_bars += 1;
    }

    fn begin_bar(&mut self, bar: Bar, bar_number: usize, position: PositionView) {
        self.open.push(bar.open);
        self.high.push(bar.high);
        self.low.push(bar.low);
        self.close.push(bar.close);
        self.volume.push(bar.volume);
        self.date.push(bar.date);
        self.time.push(bar.time);
        self.bar = bar;
        self.bar_number = bar_number;
        self.position = position;
        self.orders.clear();
        self.risk = RiskUpdate::default();
        // LoadVar is hoisted out of the specialised stream: copy
        // prev → vars here, including the f32 narrowing LoadVar used to do.
        for slot in 0..self.vars.len() {
            let prev = self.prev[slot].clone();
            let value = if self.var_numeric[slot] && !self.var_double_accum[slot] {
                match prev.as_f64() {
                    Ok(n) => Value::Num(narrow_f32(n)),
                    Err(_) => prev,
                }
            } else {
                prev
            };
            self.vars[slot] = Some(value);
        }
    }

    fn end_bar(&mut self) {
        for &slot in &self.stateful {
            if let Some(value) = self.vars[slot].clone() {
                self.prev[slot] = value.clone();
                if self.needs_value_history[slot] {
                    self.history[slot].push(value.clone());
                }
                if self.needs_numeric_history[slot] {
                    if let Ok(num) = value.as_f64() {
                        self.num_history[slot].push(num);
                    }
                }
            }
        }
        self.first_bar = false;
    }

    fn exec_program(&mut self) -> Result<(), KernelError> {
        self.stack.clear();
        let code = Arc::clone(&self.code);
        let instrs = if self.first_bar {
            &code.first
        } else {
            &code.rest
        };
        self.run(instrs)
    }

    fn run(&mut self, instrs: &[Instr]) -> Result<(), KernelError> {
        let mut pc = 0usize;
        while let Some(instr) = instrs.get(pc) {
            pc += 1;
            match instr {
                Instr::PushNum(n) => self.stack.push(Value::Num(*n)),
                Instr::PushBool(b) => self.stack.push(Value::Bool(*b)),
                Instr::PushStr(s) => self.stack.push(Value::str(s.clone())),
                Instr::PushInput(slot) => self.stack.push(self.inputs[*slot].clone()),
                Instr::PushVar(slot) => {
                    let v = self.vars[*slot].clone().ok_or_else(|| {
                        KernelError::unsupported(format!(
                            "unresolved identifier {:?}",
                            self.program.var_names[*slot]
                        ))
                    })?;
                    self.stack.push(v);
                }
                Instr::PushVarSeries(slot) => {
                    let hist = &self.history[*slot];
                    let mut series = Vec::with_capacity(hist.len() + 1);
                    for v in hist {
                        series.push(v.as_f64()?);
                    }
                    if let Some(cur) = self.vars[*slot].as_ref() {
                        series.push(cur.as_f64()?);
                    }
                    self.stack.push(Value::series(series));
                }
                Instr::PushSeriesParam(series) => {
                    let last = self.series_last(*series);
                    if series.is_ohlcv() {
                        self.stack.push(Value::Num(narrow_f32(last)));
                    } else {
                        self.stack.push(Value::Num(last));
                    }
                }
                Instr::PushSeriesParamSeries(series) => {
                    self.stack.push(Value::series(self.series_column(*series)));
                }
                Instr::PushMarketPosition => {
                    self.stack
                        .push(Value::Num(self.position.market_position as f64));
                }
                Instr::PushPositionContracts => {
                    self.stack.push(Value::Num(self.position.contracts as f64));
                }
                Instr::PushPositionKw(kind) => {
                    let v = self.position_kw_value(*kind);
                    self.stack.push(v);
                }
                Instr::PushMcKw(kind) => {
                    let v = match kind {
                        McKw::TimeS => Value::Num((self.bar.time * 100) as f64),
                        McKw::BarTypeEx => Value::Num(2.0),
                    };
                    self.stack.push(v);
                }
                Instr::Jump(target) => pc = *target,
                Instr::JumpIfFalse(target) => {
                    if !self.stack.pop().unwrap().is_truthy() {
                        pc = *target;
                    }
                }
                Instr::JumpIfTruthyPeek(target) => {
                    if self.stack.last().unwrap().is_truthy() {
                        pc = *target;
                    }
                }
                Instr::JumpIfNotTruthyPeek(target) => {
                    if !self.stack.last().unwrap().is_truthy() {
                        pc = *target;
                    }
                }
                Instr::JumpIfNotFirstBar(target) => {
                    if !self.first_bar {
                        pc = *target;
                    }
                }
                Instr::SetFirstBarFalse => self.first_bar = false,
                Instr::Drop => {
                    self.stack.pop();
                }
                Instr::Binop(op) => {
                    let right = self.stack.pop().unwrap();
                    let left = self.stack.pop().unwrap();
                    let value = self.binop(*op, left, right)?;
                    self.stack.push(value);
                }
                Instr::Compare(cmp) => {
                    let right = self.stack.pop().unwrap();
                    let left = self.stack.pop().unwrap();
                    let value = self.compare(*cmp, left, right)?;
                    self.stack.push(value);
                }
                Instr::Unary(un) => {
                    let operand = self.stack.pop().unwrap();
                    let value = match un {
                        Unary::Not => Value::Bool(!operand.is_truthy()),
                        Unary::Neg => Value::Num(-operand.as_f64()?),
                        Unary::Pos => Value::Num(operand.as_f64()?),
                    };
                    self.stack.push(value);
                }
                Instr::Call(op, nargs) => {
                    let value = self.call(*op, *nargs)?;
                    self.stack.push(value);
                }
                Instr::Average { src, kind } => {
                    let length = self.stack.pop().unwrap().as_f64()?;
                    let view = self.series_view_for(*src)?;
                    let value = pl_average(&view, length, *kind, narrow_f32)?;
                    self.stack.push(Value::Num(value));
                }
                Instr::Crosses { above, a, b } => {
                    let view_a = self.series_view_for(*a)?;
                    let view_b = self.series_view_for(*b)?;
                    let value = if *above {
                        pl_crosses_above(&view_a, &view_b)
                    } else {
                        pl_crosses_below(&view_a, &view_b)
                    };
                    self.stack.push(Value::Bool(value));
                }
                Instr::BarRefSeries(series) => {
                    let index = self.stack.pop().unwrap().as_f64()? as i64;
                    let n = (index + 1) as usize;
                    let current_open = *self.open.last().unwrap_or(&0.0);
                    let value = match series {
                        Series::Open => history_or_open(&self.open, n, current_open),
                        Series::High => history_or_open(&self.high, n, current_open),
                        Series::Low => history_or_open(&self.low, n, current_open),
                        Series::Close => history_or_open(&self.close, n, current_open),
                        Series::Volume => history_or_open(&self.volume, n, current_open),
                        Series::Date => {
                            *self.date.get(self.date.len().wrapping_sub(n)).unwrap_or(&0) as f64
                        }
                        Series::Time => {
                            *self.time.get(self.time.len().wrapping_sub(n)).unwrap_or(&0) as f64
                        }
                    };
                    // Python reads `close[-(n + 1)]` (and the `open[-1]`
                    // fallback) unrounded, even for `[0]`; only the hoisted
                    // current-bar `_close` is f32.
                    self.stack.push(Value::Num(value));
                }
                Instr::BarRefVar(slot) => {
                    let index = self.stack.pop().unwrap().as_f64()? as i64;
                    let n = index as usize;
                    if n == 0 {
                        if let Some(cur) = self.vars[*slot].clone() {
                            self.stack.push(cur);
                            continue;
                        }
                    }
                    let hist = &self.history[*slot];
                    let value = hist
                        .get(hist.len().wrapping_sub(n))
                        .or_else(|| hist.first())
                        .cloned()
                        .ok_or_else(|| {
                            KernelError::unsupported(format!(
                                "unseeded var {:?}",
                                self.program.var_names[*slot]
                            ))
                        })?;
                    self.stack.push(value);
                }
                Instr::BarRefPositionKw(kind) => {
                    self.stack.pop();
                    let v = self.position_kw_value(*kind);
                    self.stack.push(v);
                }
                Instr::PositionsAgo(field) => {
                    let index = self.stack.pop().unwrap_or(Value::Num(0.0));
                    let v = self.positions_ago(*field, &index)?;
                    self.stack.push(v);
                }
                Instr::DataOf { series, data_num } => {
                    let value = if *data_num == 1 {
                        Value::Num(self.raw_series_last(*series)?)
                    } else {
                        let aligned = self.data2.as_ref().ok_or_else(|| {
                            KernelError::unsupported(
                                "program references Data2 but no data2 series was supplied",
                            )
                        })?;
                        let idx = self.history_bars + self.bar_number.saturating_sub(1);
                        match aligned.value_at(*series, idx) {
                            Some(value) => Value::Num(value),
                            None => Value::Num(self.raw_series_last(*series)?),
                        }
                    };
                    self.stack.push(value);
                }
                Instr::LoadVar(slot) => {
                    let prev = self.prev[*slot].clone();
                    let value = if self.var_numeric[*slot] && !self.var_double_accum[*slot] {
                        Value::Num(narrow_f32(prev.as_f64()?))
                    } else {
                        prev
                    };
                    self.vars[*slot] = Some(value);
                }
                Instr::StoreVar(slot) => {
                    let mut value = self.stack.pop().unwrap();
                    if self.var_numeric[*slot] && !self.var_double_accum[*slot] {
                        value = Value::Num(narrow_f32(value.as_f64()?));
                    }
                    self.vars[*slot] = Some(value);
                }
                Instr::EmitOrder {
                    action,
                    order_type,
                    timing,
                    has_price,
                    from_entry,
                } => {
                    let price = if *has_price {
                        Some(self.stack.pop().unwrap().as_f64()?)
                    } else {
                        None
                    };
                    let signal = self.stack.pop().unwrap().into_string();
                    let quantity = self.stack.pop().unwrap().as_f64()?;
                    self.orders.push(Order {
                        action: *action,
                        order_type: *order_type,
                        quantity,
                        timing: *timing,
                        signal,
                        price,
                        from_entry: from_entry.clone(),
                    });
                }
                Instr::RiskSet(op) => match op {
                    RiskOp::StopLoss => {
                        self.risk.stop_loss = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::ProfitTarget => {
                        self.risk.profit_target = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::BreakEven => {
                        self.risk.breakeven = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::PercentTrailing => {
                        let pct = self.stack.pop().unwrap().as_f64()?;
                        let floor = self.stack.pop().unwrap().as_f64()?;
                        self.risk.percent_trailing = Some((floor, pct));
                    }
                    RiskOp::ExitOnClose => {
                        self.risk.exit_on_close = Some(true);
                    }
                    RiskOp::StopLossPt => {
                        self.risk.stop_loss_pt = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::ProfitTargetPt => {
                        self.risk.profit_target_pt = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::BreakEvenPt => {
                        self.risk.breakeven_pt = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::PercentTrailingPt => {
                        let pct = self.stack.pop().unwrap().as_f64()?;
                        let floor = self.stack.pop().unwrap().as_f64()?;
                        self.risk.percent_trailing_pt = Some((floor, pct));
                    }
                    RiskOp::DollarTrailing => {
                        self.risk.dollar_trailing = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::TrailingStopPt => {
                        self.risk.trailing_stop_pt = Some(self.stack.pop().unwrap().as_f64()?);
                    }
                    RiskOp::Noop => {}
                },
                Instr::ArrayGet(slot) => {
                    let index = self.stack.pop().unwrap();
                    let (value, kind) = self.arrays[*slot].get(&index)?;
                    self.last_read_kind = kind;
                    self.stack.push(value);
                }
                Instr::ArraySet { slot, kind } => {
                    let index = self.stack.pop().unwrap();
                    let value = self.stack.pop().unwrap();
                    let kind = self.write_kind(*kind);
                    self.arrays[*slot].set(&index, value, kind)?;
                }
                Instr::ArrayCall { op, slot, kind } => {
                    let at = self.stack.len() - usize::from(op.argc());
                    let args = self.stack.split_off(at);
                    let kind = self.write_kind(*kind);
                    let value = self.arrays[*slot].call(*op, &args, kind)?;
                    self.stack.push(value);
                }
                Instr::ArrayCopy { src, dst } => {
                    // pl_array_copy: the chunk is read before the write, so a
                    // copy within one array behaves as if through a buffer.
                    let n = self.stack.pop().unwrap();
                    let di = self.stack.pop().unwrap();
                    let si = self.stack.pop().unwrap();
                    let dst_len = self.arrays[*dst].len();
                    if let Some(chunk) = self.arrays[*src].copy_chunk(dst_len, &si, &di, &n)? {
                        self.arrays[*dst].write_chunk(chunk);
                    }
                    self.stack.push(Value::Num(0.0));
                }
                Instr::ForInit {
                    slot,
                    lp,
                    downto,
                    exit,
                } => {
                    let end = self.stack.pop().unwrap();
                    let start = self.stack.pop().unwrap();
                    let start = py_index_int(&start, "for-loop start")?;
                    let end = py_index_int(&end, "for-loop end")?;
                    if start.abs() > MAX_FOR_BOUND || end.abs() > MAX_FOR_BOUND {
                        return Err(KernelError::unsupported(format!(
                            "for-loop bounds {start}..{end} beyond ±2^24"
                        )));
                    }
                    if (*downto && start < end) || (!*downto && start > end) {
                        pc = *exit;
                    } else {
                        self.loops[*lp] = (start, end);
                        self.vars[*slot] = Some(Value::Num(start as f64));
                    }
                }
                Instr::ForNext {
                    slot,
                    lp,
                    downto,
                    body,
                } => {
                    let (current, end) = self.loops[*lp];
                    if current != end {
                        let next = if *downto { current - 1 } else { current + 1 };
                        self.loops[*lp].0 = next;
                        self.vars[*slot] = Some(Value::Num(next as f64));
                        pc = *body;
                    }
                }
                Instr::Nop => {}
                Instr::BinopC { op, rhs } => {
                    let left = pop_num(&mut self.stack);
                    self.stack.push(Value::Num(binop_num(*op, left, *rhs)?));
                }
                Instr::BinopCLhs { op, lhs } => {
                    let right = pop_num(&mut self.stack);
                    self.stack.push(Value::Num(binop_num(*op, *lhs, right)?));
                }
                Instr::CompareC { cmp, rhs } => {
                    let left = pop_num(&mut self.stack);
                    self.stack.push(Value::Bool(eval_cmp_num(*cmp, left, *rhs)));
                }
                Instr::CompareCLhs { cmp, lhs } => {
                    let right = pop_num(&mut self.stack);
                    self.stack
                        .push(Value::Bool(eval_cmp_num(*cmp, *lhs, right)));
                }
                Instr::AverageConst { src, length, kind } => {
                    let view = self.series_view_for(*src)?;
                    let value = pl_average(&view, *length, *kind, narrow_f32)?;
                    self.stack.push(Value::Num(value));
                }
                Instr::BarRefSeriesConst { series, index } => {
                    let n = (*index + 1) as usize;
                    let current_open = *self.open.last().unwrap_or(&0.0);
                    let value = match series {
                        Series::Open => history_or_open(&self.open, n, current_open),
                        Series::High => history_or_open(&self.high, n, current_open),
                        Series::Low => history_or_open(&self.low, n, current_open),
                        Series::Close => history_or_open(&self.close, n, current_open),
                        Series::Volume => history_or_open(&self.volume, n, current_open),
                        Series::Date => {
                            *self.date.get(self.date.len().wrapping_sub(n)).unwrap_or(&0) as f64
                        }
                        Series::Time => {
                            *self.time.get(self.time.len().wrapping_sub(n)).unwrap_or(&0) as f64
                        }
                    };
                    // Python reads `close[-(n + 1)]` (and the `open[-1]`
                    // fallback) unrounded, even for `[0]`; only the hoisted
                    // current-bar `_close` is f32.
                    self.stack.push(Value::Num(value));
                }
                Instr::BarRefVarConst { slot, index } => {
                    let n = *index as usize;
                    if n == 0 {
                        if let Some(cur) = self.vars[*slot].clone() {
                            self.stack.push(cur);
                            continue;
                        }
                    }
                    let hist = &self.history[*slot];
                    let value = hist
                        .get(hist.len().wrapping_sub(n))
                        .or_else(|| hist.first())
                        .cloned()
                        .ok_or_else(|| {
                            KernelError::unsupported(format!(
                                "unseeded var {:?}",
                                self.program.var_names[*slot]
                            ))
                        })?;
                    self.stack.push(value);
                }
                Instr::SeriesCompareC { series, cmp, rhs } => {
                    let left = self.series_scalar(*series);
                    self.stack.push(Value::Bool(eval_cmp_num(*cmp, left, *rhs)));
                }
                Instr::SeriesBinopC { series, op, rhs } => {
                    let left = self.series_scalar(*series);
                    self.stack.push(Value::Num(binop_num(*op, left, *rhs)?));
                }
                Instr::SeriesBinopCLhs { series, op, lhs } => {
                    let right = self.series_scalar(*series);
                    self.stack.push(Value::Num(binop_num(*op, *lhs, right)?));
                }
                Instr::VarBinopC { slot, op, rhs } => {
                    let left = var_num(&self.vars, *slot, &self.program.var_names)?;
                    self.stack.push(Value::Num(binop_num(*op, left, *rhs)?));
                }
                Instr::VarBinopCLhs { slot, op, lhs } => {
                    let right = var_num(&self.vars, *slot, &self.program.var_names)?;
                    self.stack.push(Value::Num(binop_num(*op, *lhs, right)?));
                }
                Instr::VarCompareC { slot, cmp, rhs } => {
                    let left = var_num(&self.vars, *slot, &self.program.var_names)?;
                    self.stack.push(Value::Bool(eval_cmp_num(*cmp, left, *rhs)));
                }
                Instr::VarCompareCLhs { slot, cmp, lhs } => {
                    let right = var_num(&self.vars, *slot, &self.program.var_names)?;
                    self.stack
                        .push(Value::Bool(eval_cmp_num(*cmp, *lhs, right)));
                }
                Instr::EmitOrderConst {
                    action,
                    order_type,
                    timing,
                    quantity,
                    signal,
                    from_entry,
                } => {
                    self.orders.push(Order {
                        action: *action,
                        order_type: *order_type,
                        quantity: *quantity,
                        timing: *timing,
                        signal: signal.clone(),
                        price: None,
                        from_entry: from_entry.clone(),
                    });
                }
                Instr::EmitOrderAll {
                    action,
                    order_type,
                    timing,
                    signal,
                    from_entry,
                } => {
                    self.orders.push(Order {
                        action: *action,
                        order_type: *order_type,
                        quantity: self.position.contracts as f64,
                        timing: *timing,
                        signal: signal.clone(),
                        price: None,
                        from_entry: from_entry.clone(),
                    });
                }
            }
        }
        Ok(())
    }

    /// The stored type of a written value: `Read` takes the element read last.
    fn write_kind(&self, kind: ElemKind) -> ElemKind {
        if kind == ElemKind::Read {
            self.last_read_kind
        } else {
            kind
        }
    }

    #[inline(always)]
    fn series_scalar(&self, series: Series) -> f64 {
        let last = self.series_last(series);
        if series.is_ohlcv() {
            narrow_f32(last)
        } else {
            last
        }
    }

    fn binop(&self, op: Binop, left: Value, right: Value) -> Result<Value, KernelError> {
        if op == Binop::Add && (left.is_str() || right.is_str()) {
            // Python raises TypeError on `str + number`.
            if !(left.is_str() && right.is_str()) {
                return Err(KernelError::unsupported(
                    "a string added to a non-string (Python raises TypeError)",
                ));
            }
            let mut s = left.into_string();
            s.push_str(&right.into_string());
            return Ok(Value::str(s));
        }
        let a = left.as_f64()?;
        let b = right.as_f64()?;
        Ok(Value::Num(binop_num(op, a, b)?))
    }

    fn compare(&self, cmp: Cmp, left: Value, right: Value) -> Result<Value, KernelError> {
        if let (Some(a), Some(b)) = (left.as_str(), right.as_str()) {
            let r = match cmp {
                Cmp::Eq => a == b,
                Cmp::Ne => a != b,
                Cmp::Lt => a < b,
                Cmp::Gt => a > b,
                Cmp::Le => a <= b,
                Cmp::Ge => a >= b,
            };
            return Ok(Value::Bool(r));
        }
        let a = left.as_f64()?;
        let b = right.as_f64()?;
        let r = match cmp {
            Cmp::Eq => a == b,
            Cmp::Ne => a != b,
            Cmp::Lt => a < b,
            Cmp::Gt => a > b,
            Cmp::Le => a <= b,
            Cmp::Ge => a >= b,
        };
        Ok(Value::Bool(r))
    }

    fn call(&mut self, op: CallOp, nargs: u8) -> Result<Value, KernelError> {
        let mut args = Vec::with_capacity(nargs as usize);
        for _ in 0..nargs {
            args.push(self.stack.pop().unwrap());
        }
        args.reverse();
        match op {
            CallOp::NumToStr => {
                if args.len() < 2 {
                    return Err(KernelError::unsupported("NumToStr requires 2 args"));
                }
                let x = args[0].as_f64()?;
                Ok(Value::str(num_to_str(x, args[1].as_f64()?)?))
            }
            CallOp::File => {
                // pl_file returns its argument unchanged; only a string path
                // is the kernel's string.
                match args.first() {
                    Some(path) if path.is_str() => Ok(path.clone()),
                    _ => Err(KernelError::unsupported("File of a non-string path")),
                }
            }
            CallOp::AbsValue => {
                if args.len() != 1 {
                    return Err(KernelError::unsupported("AbsValue requires 1 arg"));
                }
                Ok(Value::Num(args[0].as_f64()?.abs()))
            }
            CallOp::IntPortion => {
                if args.len() != 1 {
                    return Err(KernelError::unsupported("IntPortion requires 1 arg"));
                }
                Ok(Value::Num(py_int(args[0].as_f64()?)?))
            }
            CallOp::Sign => {
                if args.len() != 1 {
                    return Err(KernelError::unsupported("Sign requires 1 arg"));
                }
                Ok(Value::Num(py_sign(args[0].as_f64()?)))
            }
            CallOp::Square => {
                if args.len() != 1 {
                    return Err(KernelError::unsupported("Square requires 1 arg"));
                }
                Ok(Value::Num(py_square(args[0].as_f64()?)?))
            }
            CallOp::Round => match args.as_slice() {
                [x] => Ok(Value::Num(py_round(x.as_f64()?, None)?)),
                [x, n] => Ok(Value::Num(py_round(x.as_f64()?, Some(n.as_f64()?))?)),
                _ => Err(KernelError::unsupported("Round requires 1 or 2 args")),
            },
            CallOp::MaxList | CallOp::MinList => {
                let nums = args
                    .iter()
                    .map(Value::as_f64)
                    .collect::<Result<Vec<_>, _>>()?;
                Ok(Value::Num(py_extreme(&nums, op == CallOp::MaxList)?))
            }
            CallOp::TlNew => self.draw_tl_new(&args),
            CallOp::TlSetColor => {
                self.need(2, "TL_SetColor", &args)?;
                self.drawings
                    .tl_set_color(args[0].as_f64()?, args[1].as_f64()?)
                    .map(Value::Num)
            }
            CallOp::TlSetStyle => {
                self.need(2, "TL_SetStyle", &args)?;
                self.drawings
                    .tl_set_style(args[0].as_f64()?, args[1].as_f64()?)
                    .map(Value::Num)
            }
            CallOp::ArwNew => self.draw_arw_new(&args),
            CallOp::TextNew => self.draw_text_new(&args),
            CallOp::TextSetColor => {
                self.need(2, "Text_SetColor", &args)?;
                self.drawings
                    .text_set_color(args[0].as_f64()?, args[1].as_f64()?)
                    .map(Value::Num)
            }
            CallOp::StrLen => {
                self.need(1, "StrLen", &args)?;
                str_len(&args[0]).map(Value::Num)
            }
            CallOp::FormatDate => format_date_call(&args).map(Value::str),
            CallOp::FileNoop => Ok(Value::Num(0.0)),
            CallOp::Rgb => {
                self.need(3, "RGB", &args)?;
                rgb(args[0].as_f64()?, args[1].as_f64()?, args[2].as_f64()?).map(Value::Num)
            }
            CallOp::LegacyColorToRgb => {
                self.need(1, "LegacyColorToRGB", &args)?;
                legacy_color_to_rgb(args[0].as_f64()?).map(Value::Num)
            }
            CallOp::GradientColor => {
                self.need(5, "GradientColor", &args)?;
                gradient_color(
                    args[0].as_f64()?,
                    args[1].as_f64()?,
                    args[2].as_f64()?,
                    args[3].as_f64()?,
                    args[4].as_f64()?,
                )
                .map(Value::Num)
            }
            CallOp::SessionTime(hhmm) => Ok(Value::Num(f64::from(hhmm))),
            CallOp::Floor => {
                self.need(1, "Floor", &args)?;
                Ok(Value::Num(py_floor(args[0].as_f64()?)?))
            }
            CallOp::ElDateToDateTime => {
                self.need(1, "ELDateToDateTime", &args)?;
                Ok(Value::Num(py_datetojulian(args[0].as_f64()?)?))
            }
            CallOp::Clock(word) => Ok(Value::Num(clock_value(word, &(self.clock)()?))),
            CallOp::CurrentSession => iso_weekday(self.bar.date)
                .map(|d| Value::Num(f64::from(d)))
                .ok_or_else(|| KernelError::unsupported("CurrentSession on an invalid bar date")),
        }
    }

    fn need(&self, n: usize, name: &str, args: &[Value]) -> Result<(), KernelError> {
        if args.len() == n {
            Ok(())
        } else {
            Err(KernelError::unsupported(format!(
                "{name} requires {n} args"
            )))
        }
    }

    fn created_bar(&self) -> (i64, i64, i64) {
        (
            (self.history_bars + self.bar_number) as i64,
            self.bar.date,
            self.bar.time,
        )
    }

    fn draw_tl_new(&mut self, args: &[Value]) -> Result<Value, KernelError> {
        self.need(6, "TL_New", args)?;
        let a1 = find_anchor(
            &self.drawing_tape,
            args[0].as_f64()?,
            args[1].as_f64()?,
            args[2].as_f64()?,
        )?;
        let a2 = find_anchor(
            &self.drawing_tape,
            args[3].as_f64()?,
            args[4].as_f64()?,
            args[5].as_f64()?,
        )?;
        Ok(Value::Num(self.drawings.tl_new(self.created_bar(), a1, a2)))
    }

    fn draw_arw_new(&mut self, args: &[Value]) -> Result<Value, KernelError> {
        self.need(4, "Arw_New", args)?;
        let anchor = find_anchor(
            &self.drawing_tape,
            args[0].as_f64()?,
            args[1].as_f64()?,
            args[2].as_f64()?,
        )?;
        Ok(Value::Num(self.drawings.arw_new(
            self.created_bar(),
            anchor,
            args[3].is_truthy(),
        )))
    }

    fn draw_text_new(&mut self, args: &[Value]) -> Result<Value, KernelError> {
        self.need(4, "Text_New", args)?;
        let anchor = find_anchor(
            &self.drawing_tape,
            args[0].as_f64()?,
            args[1].as_f64()?,
            args[2].as_f64()?,
        )?;
        let text = args[3].clone().into_string();
        Ok(Value::Num(self.drawings.text_new(
            self.created_bar(),
            anchor,
            text,
        )))
    }

    fn position_kw_value(&self, kind: PositionKw) -> Value {
        let p = &self.position;
        match kind {
            PositionKw::Barnumber => Value::Num(self.bar_number as f64),
            PositionKw::MarketPosition => Value::Num(p.market_position as f64),
            PositionKw::CurrentContracts => Value::Num(p.contracts as f64),
            PositionKw::EntryPrice => Value::Num(p.entry_price),
            PositionKw::AvgEntryPrice => Value::Num(p.avg_entry_price),
            PositionKw::BarsSinceEntry => Value::Num(p.bars_since_entry as f64),
            PositionKw::ExitPrice => Value::Num(p.exit_price),
            PositionKw::BarsSinceExit => Value::Num(p.bars_since_exit as f64),
            PositionKw::OpenPositionProfit => Value::Num(p.open_position_profit),
            PositionKw::PositionProfit => Value::Num(p.position_profit),
            PositionKw::NetProfit => Value::Num(p.net_profit),
            PositionKw::GrossProfit => Value::Num(p.gross_profit),
            PositionKw::GrossLoss => Value::Num(p.gross_loss),
            PositionKw::TotalTrades => Value::Num(p.total_trades as f64),
            PositionKw::MaxContractsHeld => Value::Num(p.max_contracts_held as f64),
            PositionKw::MaxIdDrawdown => Value::Num(p.max_id_drawdown),
            PositionKw::CurrentDate => Value::Num(self.bar.date as f64),
            PositionKw::CurrentTime => Value::Num(self.bar.time as f64),
            PositionKw::AlertEnabled => Value::Bool(false),
            PositionKw::CheckAlert => Value::Bool(true),
            PositionKw::LastBarOnChartEx => Value::Bool(false),
            PositionKw::MinMove => Value::Num(self.minmove),
            PositionKw::PriceScale => Value::Num(self.pricescale),
            PositionKw::EntryDate => Value::Num(self.entry_date as f64),
            PositionKw::EntryTime => Value::Num(self.entry_time as f64),
            PositionKw::ExitDate | PositionKw::ExitTime => Value::Num(0.0),
            PositionKw::MaxShares => Value::Num(self.max_shares as f64),
        }
    }

    /// Python `pl_pos_ago`: `n = int(index)` (0 when that raises
    /// ValueError); `n <= 0` is the bare keyword, `n >= 1` the n-th most
    /// recently closed position, and 0 past the oldest one.
    fn positions_ago(&self, field: PosAgoField, index: &Value) -> Result<Value, KernelError> {
        if index.is_str() {
            return Err(KernelError::unsupported(
                "positions-ago keyword with a string index",
            ));
        }
        let raw = index.as_f64()?;
        if raw.is_infinite() {
            // int(inf) raises OverflowError, which pl_pos_ago does not catch.
            return Err(KernelError::unsupported(
                "positions-ago keyword with an infinite index",
            ));
        }
        // int(nan) raises ValueError, which pl_pos_ago maps to 0.
        let n = if raw.is_nan() { 0.0 } else { raw.trunc() };
        if n <= 0.0 {
            return Ok(self.position_kw_value(field.current()));
        }
        let closed = &self.closed_trades;
        if n > closed.len() as f64 {
            return Ok(Value::Num(0.0));
        }
        let trade = &closed[closed.len() - n as usize];
        Ok(Value::Num(match field {
            PosAgoField::EntryDate => trade.entry_date as f64,
            PosAgoField::EntryTime => trade.entry_time as f64,
            PosAgoField::EntryPrice => trade.entry_price,
            PosAgoField::ExitDate => trade.exit_date as f64,
            PosAgoField::ExitTime => trade.exit_time as f64,
            PosAgoField::ExitPrice => trade.exit_price,
            PosAgoField::MaxShares => trade.contracts as f64,
        }))
    }

    fn series_last(&self, series: Series) -> f64 {
        match series {
            Series::Open => *self.open.last().unwrap_or(&0.0),
            Series::High => *self.high.last().unwrap_or(&0.0),
            Series::Low => *self.low.last().unwrap_or(&0.0),
            Series::Close => *self.close.last().unwrap_or(&0.0),
            Series::Volume => *self.volume.last().unwrap_or(&0.0),
            Series::Date => *self.date.last().unwrap_or(&0) as f64,
            Series::Time => *self.time.last().unwrap_or(&0) as f64,
        }
    }

    fn series_column(&self, series: Series) -> Vec<f64> {
        match series {
            Series::Open => self.open.clone(),
            Series::High => self.high.clone(),
            Series::Low => self.low.clone(),
            Series::Close => self.close.clone(),
            Series::Volume => self.volume.clone(),
            Series::Date => self.date.iter().map(|d| *d as f64).collect(),
            Series::Time => self.time.iter().map(|t| *t as f64).collect(),
        }
    }

    fn raw_series_last(&self, series: Series) -> Result<f64, KernelError> {
        match series {
            Series::Open => Ok(*self.open.last().unwrap_or(&0.0)),
            Series::High => Ok(*self.high.last().unwrap_or(&0.0)),
            Series::Low => Ok(*self.low.last().unwrap_or(&0.0)),
            Series::Close => Ok(*self.close.last().unwrap_or(&0.0)),
            Series::Volume => Ok(*self.volume.last().unwrap_or(&0.0)),
            Series::Date => Ok(*self.date.last().unwrap_or(&0) as f64),
            Series::Time => Ok(*self.time.last().unwrap_or(&0) as f64),
        }
    }

    fn series_view_for(&self, src: SeriesSrc) -> Result<SeriesView<'_>, KernelError> {
        match src {
            SeriesSrc::Column(series) => match series {
                Series::Open => Ok(SeriesView::Slice(&self.open[..])),
                Series::High => Ok(SeriesView::Slice(&self.high[..])),
                Series::Low => Ok(SeriesView::Slice(&self.low[..])),
                Series::Close => Ok(SeriesView::Slice(&self.close[..])),
                Series::Volume => Ok(SeriesView::Slice(&self.volume[..])),
                Series::Date => Ok(SeriesView::Owned(
                    self.date.iter().map(|d| *d as f64).collect(),
                )),
                Series::Time => Ok(SeriesView::Owned(
                    self.time.iter().map(|t| *t as f64).collect(),
                )),
            },
            SeriesSrc::NumericVar(slot) => {
                let hist = &self.num_history[slot];
                match self.vars[slot].as_ref() {
                    Some(cur) => Ok(SeriesView::Chained(hist, cur.as_f64()?)),
                    None => Ok(SeriesView::Slice(hist)),
                }
            }
            SeriesSrc::ValueVar(slot) => {
                let hist = &self.history[slot];
                let mut series = Vec::with_capacity(hist.len() + 1);
                for v in hist {
                    series.push(v.as_f64()?);
                }
                if let Some(cur) = self.vars[slot].as_ref() {
                    series.push(cur.as_f64()?);
                }
                Ok(SeriesView::Owned(series))
            }
        }
    }
}

impl<'a> Interpreter<'a> {
    pub(crate) fn set_drawing_tick(&mut self, tick: f64) {
        self.drawings.set_tick(tick);
    }

    /// Index the whole run tape so an anchor on a later bar gets its `bn`.
    pub(crate) fn set_drawing_tape(
        &mut self,
        dates: &[i64],
        times: &[i64],
    ) -> Result<(), KernelError> {
        self.drawing_tape = TapeIndex::new(dates, times)?;
        Ok(())
    }

    /// The config's `minmove` and `pricescale`, which the Python bar loop
    /// passes as the `MinMove` / `PriceScale` run values.
    pub(crate) fn set_price_units(&mut self, minmove: i64, pricescale: i64) {
        self.minmove = minmove as f64;
        self.pricescale = pricescale as f64;
    }

    /// Replace the computer clock (tests pin the time with this).
    #[cfg(test)]
    pub(crate) fn set_clock(&mut self, clock: Clock) {
        self.clock = clock;
    }

    pub(crate) fn drawings_json(&self) -> Json {
        self.drawings.to_json()
    }

    /// Project one strategy variable slot into a typed trace cell. Returns
    /// `None` for an out-of-range slot; the caller renders it as `0`.
    pub(crate) fn var_value(&self, slot: usize) -> Option<&Value> {
        self.vars.get(slot).and_then(|v| v.as_ref())
    }
}

impl<'a> crate::audit::AuditSlots for Interpreter<'a> {
    fn slot_index(&self, name: &str) -> Option<usize> {
        self.program.var_names.iter().position(|n| n == name)
    }

    fn slot_f64(&self, index: usize) -> f64 {
        self.vars
            .get(index)
            .and_then(|v| v.as_ref())
            .and_then(|v| v.as_f64().ok())
            .unwrap_or(0.0)
    }

    fn slot_bool(&self, index: usize) -> bool {
        match self.vars.get(index).and_then(|v| v.as_ref()) {
            Some(Value::Bool(b)) => *b,
            Some(v) => v.is_truthy(),
            None => false,
        }
    }
}

impl<'a> NativeStrategy for Interpreter<'a> {
    fn on_bar(
        &mut self,
        bar: Bar,
        bar_number: usize,
        position: PositionView,
    ) -> Result<StrategyOutput, KernelError> {
        self.begin_bar(bar, bar_number, position);
        self.exec_program()?;
        self.end_bar();
        Ok(StrategyOutput {
            orders: std::mem::take(&mut self.orders),
            risk: std::mem::take(&mut self.risk),
        })
    }

    fn observe_trade_history(&mut self, history: TradeHistory<'_>) {
        self.entry_date = history.entry_date;
        self.entry_time = history.entry_time;
        self.max_shares = history.max_shares;
        // The ledger only grows: copy the positions closed since last bar.
        if let Some(new) = history.closed.get(self.closed_trades.len()..) {
            self.closed_trades.extend_from_slice(new);
        }
    }

    fn skip_plan(&self, tape: &BarTape) -> SkipPlan {
        super::skip::plan(&self.program, tape.dates())
    }
}

fn empty_position() -> PositionView {
    PositionView {
        market_position: 0,
        contracts: 0,
        entry_price: 0.0,
        avg_entry_price: 0.0,
        bars_since_entry: 0,
        exit_price: 0.0,
        bars_since_exit: 0,
        open_position_profit: 0.0,
        position_profit: 0.0,
        net_profit: 0.0,
        gross_profit: 0.0,
        gross_loss: 0.0,
        total_trades: 0,
        max_contracts_held: 0,
        max_id_drawdown: 0.0,
    }
}

#[inline(always)]
fn pop_num(stack: &mut Vec<Value>) -> f64 {
    match stack.pop() {
        Some(Value::Num(n)) => n,
        Some(Value::Bool(true)) => 1.0,
        Some(Value::Bool(false)) => 0.0,
        Some(other) => other.as_f64().unwrap_or(0.0),
        None => 0.0,
    }
}

#[inline(always)]
fn eval_cmp_num(cmp: Cmp, a: f64, b: f64) -> bool {
    match cmp {
        Cmp::Eq => a == b,
        Cmp::Ne => a != b,
        Cmp::Lt => a < b,
        Cmp::Gt => a > b,
        Cmp::Le => a <= b,
        Cmp::Ge => a >= b,
    }
}

fn var_num(vars: &[Option<Value>], slot: usize, names: &[String]) -> Result<f64, KernelError> {
    let v = vars.get(slot).and_then(|v| v.as_ref()).ok_or_else(|| {
        KernelError::unsupported(format!(
            "unresolved identifier {:?}",
            names.get(slot).map(String::as_str).unwrap_or("?")
        ))
    })?;
    match v {
        Value::Num(n) => Ok(*n),
        Value::Bool(true) => Ok(1.0),
        Value::Bool(false) => Ok(0.0),
        other => other.as_f64(),
    }
}

fn history_or_open(series: &[f64], offset: usize, current_open: f64) -> f64 {
    if series.len() > offset {
        series
            .get(series.len() - offset)
            .copied()
            .unwrap_or(current_open)
    } else {
        current_open
    }
}

/// Sentinel in the compact Data1→Data2 index: this Data1 bar has no Data2 match.
/// Valid Data2 indices are `0..u32::MAX`, so a tape of length `u32::MAX` or more
/// cannot be packed (refused loudly in [`align_data2`]).
const DATA2_MISSING: u32 = u32::MAX;

/// Compact Data2 alignment: one 32-bit Data2 index per Data1 bar, plus a
/// missing sentinel. The original Data2 tape is borrowed; `DataOf` loads only
/// the requested `Series` at the matched index.
pub(crate) struct Data2Aligned<'a> {
    tape: &'a BarTape,
    /// `index[i]` is the last Data2 bar whose `(date, time)` equals Data1 bar
    /// `i`, or [`DATA2_MISSING`].
    index: Vec<u32>,
    /// Wall time to build `index` (nanoseconds). Diagnostic; not a fill input.
    #[allow(dead_code)]
    setup_ns: u128,
    /// True when both tapes were non-decreasing and the two-pointer path ran.
    #[allow(dead_code)]
    used_two_pointer: bool,
}

impl<'a> Data2Aligned<'a> {
    /// Bytes owned by the alignment index (not the borrowed Data2 tape).
    #[allow(dead_code)]
    pub(crate) fn payload_bytes(&self) -> usize {
        self.index.len() * std::mem::size_of::<u32>()
    }

    #[allow(dead_code)]
    pub(crate) fn setup_ns(&self) -> u128 {
        self.setup_ns
    }

    #[allow(dead_code)]
    pub(crate) fn used_two_pointer(&self) -> bool {
        self.used_two_pointer
    }

    /// Data2 index for Data1 bar `data1_idx`, or `None` if missing / OOB.
    pub(crate) fn data2_index(&self, data1_idx: usize) -> Option<usize> {
        let packed = *self.index.get(data1_idx)?;
        if packed == DATA2_MISSING {
            None
        } else {
            Some(packed as usize)
        }
    }

    /// Load one Data2 series at the Data1 bar, or `None` if that bar has no match.
    #[inline]
    fn value_at(&self, series: Series, data1_idx: usize) -> Option<f64> {
        let j = self.data2_index(data1_idx)?;
        Some(match series {
            Series::Open => self.tape.opens()[j],
            Series::High => self.tape.highs()[j],
            Series::Low => self.tape.lows()[j],
            Series::Close => self.tape.closes()[j],
            Series::Volume => self.tape.volumes()[j],
            Series::Date => self.tape.dates()[j] as f64,
            Series::Time => self.tape.times()[j] as f64,
        })
    }
}

/// Align the Data2 tape to the primary tape by exact `(date, time)` match.
///
/// Monotonic (non-decreasing) tapes take a linear two-pointer walk. Duplicates
/// keep last-wins (the last Data2 bar at a timestamp maps onto every Data1 bar
/// with that timestamp). Non-monotonic input falls back to a timestamp HashMap
/// of indices only — never a copy of the seven columns.
pub(crate) fn align_data2<'a>(series: &BarTape, data2: &'a BarTape) -> Data2Aligned<'a> {
    let started = std::time::Instant::now();
    assert!(
        data2.len() < DATA2_MISSING as usize,
        "Data2 length {} does not fit in a 32-bit alignment index",
        data2.len()
    );
    let n = series.len();
    let mut index = vec![DATA2_MISSING; n];
    let used_two_pointer = tape_is_nondecreasing(series) && tape_is_nondecreasing(data2);
    if used_two_pointer {
        align_two_pointer(series, data2, &mut index);
    } else {
        align_hashmap(series, data2, &mut index);
    }
    Data2Aligned {
        tape: data2,
        index,
        setup_ns: started.elapsed().as_nanos(),
        used_two_pointer,
    }
}

fn tape_is_nondecreasing(tape: &BarTape) -> bool {
    let n = tape.len();
    if n <= 1 {
        return true;
    }
    let dates = tape.dates();
    let times = tape.times();
    let mut prev = (dates[0], times[0]);
    for i in 1..n {
        let key = (dates[i], times[i]);
        if key < prev {
            return false;
        }
        prev = key;
    }
    true
}

/// Two-pointer join for non-decreasing tapes. Data2 duplicates collapse to the
/// last index (last-wins); consecutive Data1 duplicates all receive that index.
fn align_two_pointer(series: &BarTape, data2: &BarTape, index: &mut [u32]) {
    let n = series.len();
    let m = data2.len();
    let s_date = series.dates();
    let s_time = series.times();
    let d_date = data2.dates();
    let d_time = data2.times();
    let mut i = 0usize;
    let mut j = 0usize;
    while i < n && j < m {
        let key = (s_date[i], s_time[i]);
        while j < m && (d_date[j], d_time[j]) < key {
            j += 1;
        }
        if j >= m {
            break;
        }
        if (d_date[j], d_time[j]) != key {
            i += 1;
            continue;
        }
        let mut last = j;
        j += 1;
        while j < m && (d_date[j], d_time[j]) == key {
            last = j;
            j += 1;
        }
        let packed = last as u32;
        while i < n && (s_date[i], s_time[i]) == key {
            index[i] = packed;
            i += 1;
        }
    }
}

/// Last-wins HashMap of `(date, time) → Data2 index`. Same semantics as the
/// original column-copying aligner; used when either tape is not monotonic.
fn align_hashmap(series: &BarTape, data2: &BarTape, index: &mut [u32]) {
    let mut by_time: HashMap<(i64, i64), u32> = HashMap::with_capacity(data2.len());
    let d_date = data2.dates();
    let d_time = data2.times();
    for j in 0..data2.len() {
        by_time.insert((d_date[j], d_time[j]), j as u32);
    }
    let s_date = series.dates();
    let s_time = series.times();
    for (i, slot) in index.iter_mut().enumerate() {
        if let Some(&j) = by_time.get(&(s_date[i], s_time[i])) {
            *slot = j;
        }
    }
}

/// ISO weekday (1 = Monday .. 7 = Sunday) of an EL date (`YYYMMDD`, year
/// counted from 1900), as `engine_dates._el_date_to_day_of_week` computes it.
/// `None` where Python's `datetime.date` would raise.
pub(crate) fn iso_weekday(el_date: i64) -> Option<u8> {
    let y = 1900 + el_date.div_euclid(10_000);
    let m = el_date.div_euclid(100).rem_euclid(100);
    let d = el_date.rem_euclid(100);
    if !(1..=9999).contains(&y) || !(1..=12).contains(&m) || d < 1 {
        return None;
    }
    let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    let days_in_month = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if d > days_in_month[(m - 1) as usize] {
        return None;
    }
    // Days since 1970-01-01 (a Thursday), Howard Hinnant's days_from_civil.
    let (yy, mm) = if m <= 2 { (y - 1, m + 9) } else { (y, m - 3) };
    let era = yy.div_euclid(400);
    let yoe = yy - era * 400;
    let doy = (153 * mm + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(((days + 3).rem_euclid(7) + 1) as u8)
}

/// Python `len(s)`: the code-point count. Python raises on a non-string.
pub(crate) fn str_len(s: &Value) -> Result<f64, KernelError> {
    s.as_str()
        .map(|s| s.chars().count() as f64)
        .ok_or_else(|| KernelError::unsupported("StrLen of a non-string"))
}

/// `pl_formatdate(*args)`: `''` with fewer than two arguments. A non-string
/// format (Python takes `str()` of it) or a non-number datetime is refused.
pub(crate) fn format_date_call(args: &[Value]) -> Result<String, KernelError> {
    if args.len() < 2 {
        return Ok(String::new());
    }
    let fmt = args[0]
        .as_str()
        .ok_or_else(|| KernelError::unsupported("FormatDate format that is not a string"))?;
    let Value::Num(dt) = args[1] else {
        return Err(KernelError::unsupported(
            "FormatDate datetime that is not a number",
        ));
    };
    format_date(fmt, dt)
}

#[cfg(test)]
mod align_tests {
    use super::*;
    use crate::types::BarTape;

    const ALL_SERIES: [Series; 7] = [
        Series::Open,
        Series::High,
        Series::Low,
        Series::Close,
        Series::Volume,
        Series::Date,
        Series::Time,
    ];

    fn tape(keys: &[(i64, i64)], close: &[f64]) -> BarTape {
        assert_eq!(keys.len(), close.len());
        let n = keys.len();
        let mut open = Vec::with_capacity(n);
        let mut high = Vec::with_capacity(n);
        let mut low = Vec::with_capacity(n);
        let mut volume = Vec::with_capacity(n);
        let mut date = Vec::with_capacity(n);
        let mut time = Vec::with_capacity(n);
        for i in 0..n {
            let (d, t) = keys[i];
            let c = close[i];
            open.push(c + 0.25);
            high.push(c + 0.5);
            low.push((c - 0.5).max(0.0));
            volume.push(100.0 + i as f64);
            date.push(d);
            time.push(t);
        }
        BarTape::from_columns(open, high, low, close.to_vec(), volume, date, time)
            .expect("test tape columns match")
    }

    /// Original last-wins HashMap of timestamps — independent of the production
    /// two-pointer / hashmap split, so a path bug cannot hide behind itself.
    fn reference_index(series: &BarTape, data2: &BarTape) -> Vec<Option<usize>> {
        let mut by_time: HashMap<(i64, i64), usize> = HashMap::with_capacity(data2.len());
        for j in 0..data2.len() {
            let bar = data2.bar(j);
            by_time.insert((bar.date, bar.time), j);
        }
        (0..series.len())
            .map(|i| {
                let bar = series.bar(i);
                by_time.get(&(bar.date, bar.time)).copied()
            })
            .collect()
    }

    fn series_value(data2: &BarTape, j: usize, kind: Series) -> f64 {
        let bar = data2.bar(j);
        match kind {
            Series::Open => bar.open,
            Series::High => bar.high,
            Series::Low => bar.low,
            Series::Close => bar.close,
            Series::Volume => bar.volume,
            Series::Date => bar.date as f64,
            Series::Time => bar.time as f64,
        }
    }

    fn assert_parity(label: &str, series: &BarTape, data2: &BarTape, want_two_pointer: bool) {
        let aligned = align_data2(series, data2);
        let expected = reference_index(series, data2);
        assert_eq!(
            aligned.payload_bytes(),
            series.len() * std::mem::size_of::<u32>(),
            "{label}: payload must be one u32 per Data1 bar"
        );
        assert_eq!(
            aligned.used_two_pointer(),
            want_two_pointer,
            "{label}: two-pointer path"
        );
        let _setup = aligned.setup_ns();
        for (i, expected_idx) in expected.iter().enumerate().take(series.len()) {
            assert_eq!(
                aligned.data2_index(i),
                *expected_idx,
                "{label}: Data2 index at Data1 {i}"
            );
            for kind in ALL_SERIES {
                let got = aligned.value_at(kind, i);
                let want = expected_idx.map(|j| series_value(data2, j, kind));
                assert_eq!(got, want, "{label}: {kind:?} at Data1 {i}");
            }
        }
    }

    fn xorshift(seed: &mut u64) -> u64 {
        let mut x = *seed;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        *seed = x;
        x
    }

    fn random_tape(n: usize, mut seed: u64, monotonic: bool, dup_every: u64) -> BarTape {
        let mut keys = Vec::with_capacity(n);
        let mut close = Vec::with_capacity(n);
        let mut day = 1180101i64;
        let mut minute = 830i64;
        for i in 0..n {
            if i > 0 && dup_every > 0 && xorshift(&mut seed).is_multiple_of(dup_every) {
                // keep the previous key (duplicate timestamp, new values)
            } else {
                minute += 1;
                if minute > 1515 {
                    minute = 830;
                    day += 1;
                }
            }
            keys.push((day, minute));
            close.push(10_000.0 + (xorshift(&mut seed) % 5000) as f64 / 4.0);
        }
        if !monotonic && n > 1 {
            for i in 0..n {
                let j = (xorshift(&mut seed) as usize) % n;
                keys.swap(i, j);
                close.swap(i, j);
            }
        }
        tape(&keys, &close)
    }

    #[test]
    fn empty_tapes_align_to_empty_index() {
        let empty = tape(&[], &[]);
        assert_parity("both empty", &empty, &empty, true);
        let one = tape(&[(1180101, 930)], &[1.0]);
        assert_parity("empty Data2", &one, &empty, true);
        assert_parity("empty Data1", &empty, &one, true);
    }

    #[test]
    fn identical_monotonic_tapes_map_identity() {
        let keys: Vec<(i64, i64)> = (0..8).map(|i| (1180101, 930 + i)).collect();
        let close: Vec<f64> = (0..8).map(|i| 100.0 + i as f64).collect();
        let t = tape(&keys, &close);
        assert_parity("identity", &t, &t, true);
        let aligned = align_data2(&t, &t);
        for i in 0..t.len() {
            assert_eq!(aligned.data2_index(i), Some(i));
        }
    }

    #[test]
    fn missing_leading_middle_and_trailing_gaps_match_hashmap() {
        let data1 = tape(
            &[
                (1180101, 930),
                (1180101, 931),
                (1180101, 932),
                (1180101, 933),
                (1180101, 934),
            ],
            &[1.0, 2.0, 3.0, 4.0, 5.0],
        );
        let data2 = tape(&[(1180101, 931), (1180101, 933)], &[20.0, 40.0]);
        assert_parity("gaps", &data1, &data2, true);
        let aligned = align_data2(&data1, &data2);
        assert_eq!(aligned.data2_index(0), None);
        assert_eq!(aligned.data2_index(1), Some(0));
        assert_eq!(aligned.data2_index(2), None);
        assert_eq!(aligned.data2_index(3), Some(1));
        assert_eq!(aligned.data2_index(4), None);
        assert_eq!(aligned.value_at(Series::Close, 1), Some(20.0));
        assert_eq!(aligned.value_at(Series::Close, 3), Some(40.0));
        assert_eq!(aligned.value_at(Series::Close, 0), None);
    }

    #[test]
    fn data2_duplicates_are_last_wins_on_two_pointer_and_hashmap() {
        let data1 = tape(
            &[
                (1180101, 930),
                (1180101, 931),
                (1180101, 931),
                (1180101, 932),
            ],
            &[1.0, 2.0, 3.0, 4.0],
        );
        // Three Data2 bars at 931; last-wins is index 3 (close 99).
        let data2 = tape(
            &[
                (1180101, 930),
                (1180101, 931),
                (1180101, 931),
                (1180101, 931),
                (1180101, 932),
            ],
            &[10.0, 21.0, 22.0, 99.0, 40.0],
        );
        assert_parity("monotonic duplicates", &data1, &data2, true);
        let aligned = align_data2(&data1, &data2);
        assert_eq!(aligned.data2_index(0), Some(0));
        assert_eq!(aligned.data2_index(1), Some(3));
        assert_eq!(aligned.data2_index(2), Some(3));
        assert_eq!(aligned.data2_index(3), Some(4));
        assert_eq!(aligned.value_at(Series::Close, 1), Some(99.0));
        assert_eq!(aligned.value_at(Series::Close, 2), Some(99.0));
    }

    #[test]
    fn unsorted_data2_falls_back_to_hashmap_and_matches_last_wins() {
        let data1 = tape(
            &[(1180101, 930), (1180101, 931), (1180101, 932)],
            &[1.0, 2.0, 3.0],
        );
        // Reverse-sorted unique keys: HashMap last-wins equals the unique match.
        let reversed = tape(
            &[(1180101, 932), (1180101, 931), (1180101, 930)],
            &[30.0, 20.0, 10.0],
        );
        assert_parity("reversed Data2", &data1, &reversed, false);
        let aligned = align_data2(&data1, &reversed);
        assert_eq!(aligned.value_at(Series::Close, 0), Some(10.0));
        assert_eq!(aligned.value_at(Series::Close, 1), Some(20.0));
        assert_eq!(aligned.value_at(Series::Close, 2), Some(30.0));

        // Unsorted duplicates: the LAST occurrence in Data2 wins (index 3, close 77).
        let dups = tape(
            &[
                (1180101, 931),
                (1180101, 930),
                (1180101, 932),
                (1180101, 931),
            ],
            &[21.0, 10.0, 30.0, 77.0],
        );
        assert_parity("unsorted Data2 duplicates", &data1, &dups, false);
        let aligned = align_data2(&data1, &dups);
        assert_eq!(aligned.data2_index(1), Some(3));
        assert_eq!(aligned.value_at(Series::Close, 1), Some(77.0));
    }

    #[test]
    fn unsorted_data1_falls_back_to_hashmap() {
        let data1 = tape(
            &[(1180101, 932), (1180101, 930), (1180101, 931)],
            &[3.0, 1.0, 2.0],
        );
        let data2 = tape(
            &[(1180101, 930), (1180101, 931), (1180101, 932)],
            &[10.0, 20.0, 30.0],
        );
        assert_parity("unsorted Data1", &data1, &data2, false);
        let aligned = align_data2(&data1, &data2);
        assert_eq!(aligned.value_at(Series::Close, 0), Some(30.0));
        assert_eq!(aligned.value_at(Series::Close, 1), Some(10.0));
        assert_eq!(aligned.value_at(Series::Close, 2), Some(20.0));
    }

    #[test]
    fn data2_longer_than_data1_and_extra_keys_are_ignored() {
        let data1 = tape(&[(1180101, 931), (1180101, 933)], &[2.0, 4.0]);
        let data2 = tape(
            &[
                (1180101, 930),
                (1180101, 931),
                (1180101, 932),
                (1180101, 933),
                (1180101, 934),
            ],
            &[10.0, 20.0, 30.0, 40.0, 50.0],
        );
        assert_parity("Data2 longer", &data1, &data2, true);
        let aligned = align_data2(&data1, &data2);
        assert_eq!(aligned.data2_index(0), Some(1));
        assert_eq!(aligned.data2_index(1), Some(3));
    }

    #[test]
    fn payload_is_one_u32_per_data1_bar_on_a_wide_tape() {
        let n = 10_000usize;
        let data1 = random_tape(n, 0x1111, true, 0);
        let data2 = random_tape(n / 2, 0x2222, true, 7);
        let aligned = align_data2(&data1, &data2);
        assert_eq!(aligned.payload_bytes(), n * 4);
        assert!(aligned.used_two_pointer());
        let _ = aligned.setup_ns();
        assert_parity("wide monotonic", &data1, &data2, true);
    }

    #[test]
    fn random_monotonic_duplicate_and_unsorted_fixtures_match_hashmap() {
        for seed in 1u64..=40 {
            let n1 = 20 + (seed as usize % 30);
            let n2 = 10 + (seed as usize % 40);
            let mono1 = random_tape(n1, seed, true, 5);
            let mono2 = random_tape(n2, seed.wrapping_mul(17), true, 4);
            assert_parity(&format!("mono seed {seed}"), &mono1, &mono2, true);

            let dups1 = random_tape(n1, seed.wrapping_add(99), true, 3);
            let dups2 = random_tape(n2, seed.wrapping_add(123), true, 2);
            assert_parity(&format!("dups seed {seed}"), &dups1, &dups2, true);

            let shuf2 = random_tape(n2, seed.wrapping_mul(31), false, 4);
            assert_parity(&format!("unsorted d2 seed {seed}"), &mono1, &shuf2, false);

            let shuf1 = random_tape(n1, seed.wrapping_mul(41), false, 0);
            assert_parity(&format!("unsorted d1 seed {seed}"), &shuf1, &mono2, false);
        }
    }
}

#[cfg(test)]
mod builtin_tests {
    use super::super::clock::ClockNow;
    use super::super::compile::compile;
    use super::*;
    use crate::audit::AuditSlots;
    use crate::types::{BarTape, Instrument};
    use serde_json::json;

    fn fixed_clock() -> Result<ClockNow, KernelError> {
        Ok(ClockNow {
            year: 2024,
            month: 3,
            day: 5,
            hour: 9,
            minute: 7,
            second: 3,
            microsecond: 250_000,
        })
    }

    fn num(v: &str) -> Json {
        json!({"type": "number", "value": v})
    }

    /// Runs `assigns` (name, expression) on two bars and returns each
    /// variable's final value.
    fn run(assigns: &[(&str, Json)]) -> Vec<Value> {
        let decls: Vec<Json> = assigns
            .iter()
            .map(|(name, _)| json!({"name": name, "init": num("0")}))
            .collect();
        let mut body = vec![json!({"type": "var_decl", "decls": decls})];
        for (name, value) in assigns {
            body.push(json!({
                "type": "assign",
                "target": {"type": "ident", "name": name},
                "value": value,
            }));
        }
        let ast = json!({"type": "program", "body": body});
        let program = compile(&ast, &HashMap::new()).unwrap();
        let mut interp = Interpreter::from_program_with(program, None, true).unwrap();
        interp.set_clock(fixed_clock);
        let tape = BarTape::from_columns(
            vec![100.0; 2],
            vec![101.0; 2],
            vec![99.0; 2],
            vec![-2.5, 1240305.0],
            vec![1.0; 2],
            vec![1240304, 1240305],
            vec![1000; 2],
        )
        .unwrap();
        crate::kernel::run_native_from(&tape, Instrument::default(), &mut interp, 0).unwrap();
        assigns
            .iter()
            .map(|(name, _)| {
                let slot = interp.slot_index(name).unwrap();
                interp.var_value(slot).cloned().unwrap()
            })
            .collect()
    }

    fn call(name: &str, args: Vec<Json>) -> Json {
        json!({"type": "call", "name": name, "args": args})
    }

    fn ident(name: &str) -> Json {
        json!({"type": "ident", "name": name})
    }

    #[test]
    fn positions_ago_follows_pl_pos_ago() {
        let ast = json!({"type": "program", "body": []});
        let program = compile(&ast, &HashMap::new()).unwrap();
        let mut interp = Interpreter::from_program_with(program, None, true).unwrap();
        let trade = |entry_date, exit_price| ClosedTrade {
            entry_date,
            entry_time: 930,
            entry_price: 10.5,
            exit_date: 1240103,
            exit_time: 1000,
            exit_price,
            contracts: 2,
        };
        let closed = [trade(1240101, 11.0), trade(1240102, 12.0)];
        interp.observe_trade_history(TradeHistory {
            entry_date: 1240104,
            entry_time: 945,
            max_shares: 3,
            closed: &closed,
        });
        fn at(interp: &Interpreter<'_>, field: PosAgoField, index: f64) -> f64 {
            interp
                .positions_ago(field, &Value::Num(index))
                .unwrap()
                .as_f64()
                .unwrap()
        }
        // n <= 0 (after truncation, NaN as 0) is the current position.
        assert_eq!(at(&interp, PosAgoField::EntryDate, 0.0), 1240104.0);
        assert_eq!(at(&interp, PosAgoField::EntryDate, 0.9), 1240104.0);
        assert_eq!(at(&interp, PosAgoField::EntryDate, -2.0), 1240104.0);
        assert_eq!(at(&interp, PosAgoField::EntryTime, f64::NAN), 945.0);
        assert_eq!(at(&interp, PosAgoField::MaxShares, 0.0), 3.0);
        assert_eq!(at(&interp, PosAgoField::ExitDate, 0.0), 0.0);
        // n >= 1 counts back from the most recently closed position.
        assert_eq!(at(&interp, PosAgoField::EntryDate, 1.0), 1240102.0);
        assert_eq!(at(&interp, PosAgoField::ExitPrice, 1.7), 12.0);
        assert_eq!(at(&interp, PosAgoField::ExitPrice, 2.0), 11.0);
        assert_eq!(at(&interp, PosAgoField::MaxShares, 2.0), 2.0);
        assert_eq!(at(&interp, PosAgoField::ExitTime, 2.0), 1000.0);
        // Past the oldest one is 0.
        assert_eq!(at(&interp, PosAgoField::EntryPrice, 3.0), 0.0);
        assert!(interp
            .positions_ago(PosAgoField::EntryDate, &Value::Num(f64::INFINITY))
            .is_err());
        // The ledger only grows: a later bar appends without duplicating.
        let more = [closed[0], closed[1], trade(1240105, 13.0)];
        interp.observe_trade_history(TradeHistory {
            entry_date: 0,
            entry_time: 0,
            max_shares: 0,
            closed: &more,
        });
        assert_eq!(at(&interp, PosAgoField::ExitPrice, 1.0), 13.0);
        assert_eq!(at(&interp, PosAgoField::ExitPrice, 3.0), 11.0);
        assert_eq!(at(&interp, PosAgoField::ExitPrice, 4.0), 0.0);
    }

    #[test]
    fn floor_eldate_and_clock_words_run_natively() {
        let got = run(&[
            (
                "a",
                call(
                    "floor",
                    vec![json!({"type": "unaryop", "op": "-", "operand": num("2.5")})],
                ),
            ),
            ("b", call("floor", vec![ident("close")])),
            ("c", call("eldatetodatetime", vec![ident("date")])),
            ("d", call("eldatetodatetime", vec![num("124"), num("305")])),
            ("e", ident("currenttime_s")),
            ("f", call("currenttime_s", vec![])),
            ("g", ident("currentdate_s")),
            ("h", ident("computerdatetime")),
        ]);
        let want: [f64; 8] = [
            -3.0,
            1_240_305.0,
            45_356.0,
            1_240_305.0,
            90_703.0,
            90_703.0,
            1_240_305.0,
            // Variables hold float32, as on the Python path.
            f64::from(45_356.379_898_726_85_f64 as f32),
        ];
        for (value, want) in got.iter().zip(want) {
            assert_eq!(
                value.as_f64().unwrap().to_bits(),
                want.to_bits(),
                "{value:?}"
            );
        }
    }

    #[test]
    fn floor_of_a_non_finite_value_refuses() {
        let ast = json!({"type": "program", "body": [
            {"type": "var_decl", "decls": [{"name": "a", "init": num("0")}]},
            {"type": "assign", "target": ident("a"),
             "value": call("floor", vec![json!({"type": "binop", "op": "*", "left": num("1e308"), "right": ident("close")})])},
        ]});
        let program = compile(&ast, &HashMap::new()).unwrap();
        let mut interp = Interpreter::from_program_with(program, None, true).unwrap();
        let tape = BarTape::from_columns(
            vec![1.0],
            vec![1.0],
            vec![1.0],
            vec![100.0],
            vec![1.0],
            vec![1240305],
            vec![1000],
        )
        .unwrap();
        assert!(
            crate::kernel::run_native_from(&tape, Instrument::default(), &mut interp, 0).is_err()
        );
    }
}
