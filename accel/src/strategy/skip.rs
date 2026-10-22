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

//! Skip-region proof for the native bar loop (L1).
//!
//! A skipped interval is only legal if we can *prove* it cannot emit an order.
//! When the proof does not go through, the kernel evaluates every bar and
//! prints the reason — never a silent full-eval fallback, never a skip that
//! would have traded.

use super::compile::{CallOp, Cmp, Instr, PositionKw, Program, Series};
use super::pynum::may_raise;
use super::value::Value;
use crate::types::SkipPlan;

const MAX_ABS_STEPS: usize = 50_000;
const MAX_BAR_FIXPOINT: usize = 32;

/// Plan a prefix skip for `program` over a tape's columnar `dates`.
pub(crate) fn plan(program: &Program, dates: &[i64]) -> SkipPlan {
    if dates.is_empty() {
        return SkipPlan::skip(0, "empty tape");
    }
    // Drawing and colour calls change the run even on bars that cannot
    // trade. A prefix skip (including "no orders, skip the tape") would
    // drop those objects. The proof does not track them, so evaluate
    // every bar.
    if program.instrs.iter().any(|instr| {
        matches!(
            instr,
            Instr::Call(
                CallOp::TlNew
                    | CallOp::TlSetColor
                    | CallOp::TlSetStyle
                    | CallOp::ArwNew
                    | CallOp::TextNew
                    | CallOp::Rgb
                    | CallOp::LegacyColorToRgb
                    | CallOp::GradientColor,
                _
            )
        )
    }) {
        return SkipPlan::unproven(
            "drawing or colour calls have side effects; cannot skip a prefix",
        );
    }
    // A skipped bar is never evaluated, so an operation the Python path raises
    // on (a zero divisor, IntPortion of a non-finite value, ...) would go
    // unreported there. Evaluate every bar unless each one is proven safe.
    if program
        .instrs
        .iter()
        .enumerate()
        .any(|(pc, instr)| may_raise(instr, pc.checked_sub(1).map(|p| &program.instrs[p])))
    {
        return SkipPlan::unproven(
            "an operation the Python path can raise on (zero divisor, IntPortion, Square, \
             MaxList/MinList); cannot skip a prefix",
        );
    }
    if !program.instrs.iter().any(|instr| {
        matches!(
            instr,
            Instr::EmitOrder { .. } | Instr::EmitOrderConst { .. } | Instr::EmitOrderAll { .. }
        )
    }) {
        return SkipPlan::skip(dates.len(), "program emits no orders");
    }
    if program
        .instrs
        .iter()
        .any(|instr| matches!(instr, Instr::JumpIfNotFirstBar(_)))
    {
        return SkipPlan::unproven(
            "Once/first-bar control flow; cannot skip a prefix without moving Once",
        );
    }
    let lookback = match series_lookback(program) {
        Ok(n) => n,
        Err(reason) => return SkipPlan::unproven(reason),
    };
    let mut candidates = date_lower_bounds(program);
    if candidates.is_empty() {
        return SkipPlan::unproven(
            "no Date>= / Date> bound dominates entries; cannot prove a dead prefix",
        );
    }
    candidates.sort_by(|a, b| {
        b.bound
            .cmp(&a.bound)
            .then_with(|| b.inclusive.cmp(&a.inclusive))
    });
    let session_depth = session_copy_depth(program);
    for cand in &candidates {
        if !orders_unreachable_below(program, cand) {
            continue;
        }
        let first = match first_in_range(dates, cand) {
            Some(i) => i,
            None => {
                return SkipPlan::skip(
                    dates.len(),
                    format!(
                        "Date {} {}; whole tape is below the bound",
                        if cand.inclusive { ">=" } else { ">" },
                        cand.bound
                    ),
                );
            }
        };
        let mut eval_from = n_sessions_before(dates, first, session_depth);
        let lookback_from = first.saturating_sub(lookback);
        if lookback_from < eval_from {
            eval_from = lookback_from;
        }
        let skipped = eval_from;
        if skipped == 0 {
            return SkipPlan::skip(
                0,
                format!(
                    "Date {} {} but the tape is already inside the window",
                    if cand.inclusive { ">=" } else { ">" },
                    cand.bound
                ),
            );
        }
        return SkipPlan::skip(
            eval_from,
            format!(
                "Date {} {}; {}-session warmup; lookback {}; skipped {skipped}",
                if cand.inclusive { ">=" } else { ">" },
                cand.bound,
                session_depth,
                lookback
            ),
        );
    }
    SkipPlan::unproven(
        "Date lower-bound candidates exist but abstract interpretation could not prove orders unreachable",
    )
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct DateLower {
    bound: i64,
    inclusive: bool,
}

fn whole_i64(n: f64) -> Option<i64> {
    if n.is_finite() && n.fract() == 0.0 && n >= i64::MIN as f64 && n <= i64::MAX as f64 {
        Some(n as i64)
    } else {
        None
    }
}

fn const_from(program: &Program, instr: &Instr) -> Option<i64> {
    match instr {
        Instr::PushNum(n) => whole_i64(*n),
        Instr::PushInput(slot) => program
            .inputs
            .get(*slot)
            .and_then(|v| v.as_f64().ok())
            .and_then(whole_i64),
        _ => None,
    }
}

fn is_date_push(instr: &Instr) -> bool {
    matches!(
        instr,
        Instr::PushSeriesParam(Series::Date) | Instr::PushPositionKw(PositionKw::CurrentDate)
    )
}

fn date_lower_bounds(program: &Program) -> Vec<DateLower> {
    let instrs = &program.instrs;
    let mut out = Vec::new();
    for i in 0..instrs.len().saturating_sub(2) {
        if let Instr::SeriesCompareC {
            series: Series::Date,
            cmp,
            rhs,
        } = &instrs[i]
        {
            if let Some(k) = whole_i64(*rhs) {
                match cmp {
                    Cmp::Ge => out.push(DateLower {
                        bound: k,
                        inclusive: true,
                    }),
                    Cmp::Gt => out.push(DateLower {
                        bound: k,
                        inclusive: false,
                    }),
                    _ => {}
                }
            }
        }
        if is_date_push(&instrs[i]) {
            if let Some(k) = const_from(program, &instrs[i + 1]) {
                if let Instr::Compare(cmp) = instrs[i + 2] {
                    match cmp {
                        Cmp::Ge => out.push(DateLower {
                            bound: k,
                            inclusive: true,
                        }),
                        Cmp::Gt => out.push(DateLower {
                            bound: k,
                            inclusive: false,
                        }),
                        _ => {}
                    }
                }
            }
        }
        if let Some(k) = const_from(program, &instrs[i]) {
            if is_date_push(&instrs[i + 1]) {
                if let Instr::Compare(cmp) = instrs[i + 2] {
                    match cmp {
                        Cmp::Le => out.push(DateLower {
                            bound: k,
                            inclusive: true,
                        }),
                        Cmp::Lt => out.push(DateLower {
                            bound: k,
                            inclusive: false,
                        }),
                        _ => {}
                    }
                }
            }
        }
    }
    out
}

fn series_lookback(program: &Program) -> Result<usize, &'static str> {
    let instrs = &program.instrs;
    let mut max_lb = 0usize;
    let mut stack: Vec<Option<i64>> = Vec::new();
    for instr in instrs {
        match instr {
            Instr::PushNum(n) => stack.push(whole_i64(*n)),
            Instr::PushBool(_) | Instr::PushStr(_) => stack.push(None),
            Instr::PushInput(slot) => stack.push(
                program
                    .inputs
                    .get(*slot)
                    .and_then(|v| v.as_f64().ok())
                    .and_then(whole_i64),
            ),
            Instr::Average(_) => {
                let length = stack.pop().flatten();
                match length {
                    Some(n) if n >= 0 => max_lb = max_lb.max(n as usize),
                    _ => return Err("Average length is not a proven constant; cannot skip"),
                }
                stack.push(None);
            }
            Instr::AverageConst { length, .. } => {
                match whole_i64(*length) {
                    Some(n) if n >= 0 => max_lb = max_lb.max(n as usize),
                    _ => return Err("Average length is not a proven constant; cannot skip"),
                }
                stack.push(None);
            }
            Instr::BarRefSeriesConst { index, .. } | Instr::BarRefVarConst { index, .. } => {
                match *index {
                    n if n >= 0 => max_lb = max_lb.max(n as usize),
                    _ => return Err("bar-ref index is not a proven constant; cannot skip"),
                }
                stack.push(None);
            }
            Instr::Crosses { .. } => {
                max_lb = max_lb.max(2);
                stack.push(None);
            }
            Instr::BarRefSeries(_) | Instr::BarRefVar(_) | Instr::BarRefPositionKw(_) => {
                let index = stack.pop().flatten();
                match index {
                    Some(n) if n >= 0 => max_lb = max_lb.max(n as usize),
                    _ => return Err("bar-ref index is not a proven constant; cannot skip"),
                }
                stack.push(None);
            }
            Instr::JumpIfFalse(_)
            | Instr::Jump(_)
            | Instr::JumpIfTruthyPeek(_)
            | Instr::JumpIfNotTruthyPeek(_)
            | Instr::JumpIfNotFirstBar(_)
            | Instr::SetFirstBarFalse
            | Instr::Drop
            | Instr::LoadVar(_)
            | Instr::StoreVar(_)
            | Instr::EmitOrder { .. }
            | Instr::EmitOrderConst { .. }
            | Instr::EmitOrderAll { .. }
            | Instr::RiskSet(_)
            | Instr::Nop => {
                // Control / effect opcodes do not push a value. Keep the
                // constant stack best-effort; a height mismatch just yields
                // None lengths later (fail-loud via Average/BarRef).
            }
            Instr::Binop(_) | Instr::Compare(_) => {
                let _ = stack.pop();
                let _ = stack.pop();
                stack.push(None);
            }
            Instr::BinopC { .. }
            | Instr::BinopCLhs { .. }
            | Instr::CompareC { .. }
            | Instr::CompareCLhs { .. } => {
                let _ = stack.pop();
                stack.push(None);
            }
            Instr::Unary(_) | Instr::Call(_, _) => {
                // Call pops nargs; we don't track nargs on this stack. Flush.
                stack.clear();
                stack.push(None);
            }
            _ => stack.push(None),
        }
    }
    Ok(max_lb)
}

/// Longest var-to-var copy chain in a `Date <> SessionDate` then-block.
///
/// The reference strategy copies SessionClose → Prior → TwoAgo (depth 2). Over-warming is
/// safe; under-warming is a missed fill. If no session rollover is found, 0.
fn session_rollover_then_ranges(program: &Program) -> Vec<(usize, usize)> {
    let instrs = &program.instrs;
    let mut ranges = Vec::new();
    for i in 0..instrs.len().saturating_sub(3) {
        if !is_date_push(&instrs[i]) {
            continue;
        }
        let Instr::PushVar(_) = instrs[i + 1] else {
            continue;
        };
        let Instr::Compare(Cmp::Ne) = instrs[i + 2] else {
            continue;
        };
        let Instr::JumpIfFalse(else_start) = instrs[i + 3] else {
            continue;
        };
        if else_start > instrs.len() {
            continue;
        }
        let then_end = instrs.get(else_start.saturating_sub(1)).map(|ins| {
            if let Instr::Jump(_) = ins {
                else_start - 1
            } else {
                else_start
            }
        });
        let Some(then_end) = then_end else { continue };
        let then_start = i + 4;
        if then_start < then_end {
            ranges.push((then_start, then_end));
        }
    }
    ranges
}

fn session_copy_depth(program: &Program) -> usize {
    let instrs = &program.instrs;
    let mut depth = 0usize;
    for (then_start, then_end) in session_rollover_then_ranges(program) {
        depth = depth.max(copy_chain_depth(&instrs[then_start..then_end], 0));
    }
    depth
}

fn copy_chain_depth(then: &[Instr], _session_date_slot: usize) -> usize {
    // dst <- src for simple `var_a = var_b` (PushVar; StoreVar).
    let mut edges: Vec<(usize, usize)> = Vec::new();
    for w in then.windows(2) {
        if let (Instr::PushVar(src), Instr::StoreVar(dst)) = (&w[0], &w[1]) {
            if src != dst {
                edges.push((*dst, *src));
            }
        }
    }
    if edges.is_empty() {
        return 0;
    }
    let mut best = 1usize;
    for &(start, _) in &edges {
        let mut seen = vec![start];
        let mut cur = start;
        let mut hops = 0usize;
        while let Some((_, src)) = edges.iter().find(|(d, _)| *d == cur) {
            hops += 1;
            if hops > 16 || seen.contains(src) {
                break;
            }
            seen.push(*src);
            cur = *src;
        }
        best = best.max(hops);
    }
    best
}

fn first_in_range(dates: &[i64], cand: &DateLower) -> Option<usize> {
    dates.iter().position(|&d| {
        if cand.inclusive {
            d >= cand.bound
        } else {
            d > cand.bound
        }
    })
}

fn session_start(dates: &[i64], mut i: usize) -> usize {
    let d = dates[i];
    while i > 0 && dates[i - 1] == d {
        i -= 1;
    }
    i
}

fn n_sessions_before(dates: &[i64], from: usize, n: usize) -> usize {
    let mut i = session_start(dates, from);
    for _ in 0..n {
        if i == 0 {
            return 0;
        }
        i = session_start(dates, i - 1);
    }
    i
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Abs {
    Top,
    Num(f64),
    Bool(bool),
    /// Integer bar date known to be `<= max_date` (the skip prefix).
    DateLe(i64),
}

impl Abs {
    fn merge(self, other: Self) -> Self {
        if self == other {
            self
        } else {
            Abs::Top
        }
    }

    fn is_truthy(self) -> Option<bool> {
        match self {
            Abs::Bool(b) => Some(b),
            Abs::Num(n) => Some(n != 0.0),
            Abs::DateLe(_) => Some(true),
            Abs::Top => None,
        }
    }
}

fn from_value(v: &Value) -> Abs {
    match v {
        Value::Num(n) => Abs::Num(*n),
        Value::Bool(b) => Abs::Bool(*b),
        Value::NonNum(_) => Abs::Top,
    }
}

fn compare_abs(cmp: Cmp, left: Abs, right: Abs) -> Abs {
    if let (Abs::DateLe(max_date), Abs::Num(k)) = (left, right) {
        if let Some(k) = whole_i64(k) {
            return date_le_cmp(cmp, max_date, k);
        }
    }
    if let (Abs::Num(k), Abs::DateLe(max_date)) = (left, right) {
        if let Some(k) = whole_i64(k) {
            return date_le_cmp(swap_cmp(cmp), max_date, k);
        }
    }
    match (left, right) {
        (Abs::Num(a), Abs::Num(b)) => Abs::Bool(match cmp {
            Cmp::Eq => a == b,
            Cmp::Ne => a != b,
            Cmp::Lt => a < b,
            Cmp::Gt => a > b,
            Cmp::Le => a <= b,
            Cmp::Ge => a >= b,
        }),
        (Abs::Bool(a), Abs::Bool(b)) => Abs::Bool(match cmp {
            Cmp::Eq => a == b,
            Cmp::Ne => a != b,
            _ => return Abs::Top,
        }),
        _ => Abs::Top,
    }
}

fn swap_cmp(cmp: Cmp) -> Cmp {
    match cmp {
        Cmp::Lt => Cmp::Gt,
        Cmp::Gt => Cmp::Lt,
        Cmp::Le => Cmp::Ge,
        Cmp::Ge => Cmp::Le,
        other => other,
    }
}

/// `left` is a date known `<= max_date`. Compare against integer `k`.
fn date_le_cmp(cmp: Cmp, max_date: i64, k: i64) -> Abs {
    match cmp {
        Cmp::Ge => {
            if k > max_date {
                Abs::Bool(false)
            } else {
                Abs::Top
            }
        }
        Cmp::Gt => {
            if k >= max_date {
                Abs::Bool(false)
            } else {
                Abs::Top
            }
        }
        Cmp::Le => {
            if k >= max_date {
                Abs::Bool(true)
            } else {
                Abs::Top
            }
        }
        Cmp::Lt => {
            if k > max_date {
                Abs::Bool(true)
            } else {
                Abs::Top
            }
        }
        Cmp::Eq => {
            if k > max_date {
                Abs::Bool(false)
            } else {
                Abs::Top
            }
        }
        Cmp::Ne => {
            if k > max_date {
                Abs::Bool(true)
            } else {
                Abs::Top
            }
        }
    }
}

#[derive(Clone, PartialEq)]
struct Frame {
    stack: Vec<Abs>,
    vars: Vec<Abs>,
}

fn merge_frame(a: &Frame, b: &Frame) -> Option<Frame> {
    if a.stack.len() != b.stack.len() || a.vars.len() != b.vars.len() {
        return None;
    }
    Some(Frame {
        stack: a
            .stack
            .iter()
            .zip(b.stack.iter())
            .map(|(x, y)| x.merge(*y))
            .collect(),
        vars: a
            .vars
            .iter()
            .zip(b.vars.iter())
            .map(|(x, y)| x.merge(*y))
            .collect(),
    })
}

struct BarAbs {
    order: bool,
    double_accum: bool,
    failed: bool,
    hist: Vec<Abs>,
}

fn interpret_bar(
    program: &Program,
    hist: &[Abs],
    max_date: i64,
    first_bar: bool,
    market_position: Abs,
    session_then: &[(usize, usize)],
) -> BarAbs {
    let instrs = &program.instrs;
    let n = instrs.len();
    let mut at: Vec<Option<Frame>> = vec![None; n + 1];
    let init = Frame {
        stack: Vec::new(),
        vars: hist.to_vec(),
    };
    at[0] = Some(init);
    let mut work = vec![0usize];
    let mut steps = 0usize;
    let mut order = false;
    let mut double_accum = false;
    let mut failed = false;

    while let Some(pc) = work.pop() {
        steps += 1;
        if steps > MAX_ABS_STEPS {
            failed = true;
            break;
        }
        if pc == n {
            continue;
        }
        let Some(frame) = at[pc].clone() else {
            continue;
        };
        let mut stack = frame.stack;
        let mut vars = frame.vars;
        let mut succs: Vec<(usize, Frame)> = Vec::new();
        match &instrs[pc] {
            Instr::PushNum(v) => {
                stack.push(Abs::Num(*v));
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushBool(b) => {
                stack.push(Abs::Bool(*b));
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushStr(_) => {
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushInput(slot) => {
                let v = program
                    .inputs
                    .get(*slot)
                    .map(from_value)
                    .unwrap_or(Abs::Top);
                stack.push(v);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushVar(slot) | Instr::PushVarSeries(slot) => {
                stack.push(vars.get(*slot).copied().unwrap_or(Abs::Top));
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushSeriesParam(series) => {
                stack.push(if *series == Series::Date {
                    Abs::DateLe(max_date)
                } else {
                    Abs::Top
                });
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushSeriesParamSeries(_) => {
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushMarketPosition | Instr::PushPositionContracts => {
                stack.push(market_position);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushPositionKw(kind) => {
                stack.push(match kind {
                    PositionKw::CurrentDate => Abs::DateLe(max_date),
                    PositionKw::MarketPosition | PositionKw::CurrentContracts => market_position,
                    PositionKw::Barnumber => Abs::Top,
                    _ => Abs::Top,
                });
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::PushMcKw(_) => {
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Jump(target) => {
                succs.push((*target, Frame { stack, vars }));
            }
            Instr::JumpIfFalse(target) => {
                let cond = stack.pop().unwrap_or(Abs::Top);
                match cond.is_truthy() {
                    Some(false) => succs.push((*target, Frame { stack, vars })),
                    Some(true) => succs.push((pc + 1, Frame { stack, vars })),
                    None => {
                        succs.push((
                            *target,
                            Frame {
                                stack: stack.clone(),
                                vars: vars.clone(),
                            },
                        ));
                        succs.push((pc + 1, Frame { stack, vars }));
                    }
                }
            }
            Instr::JumpIfTruthyPeek(target) => {
                let cond = stack.last().copied().unwrap_or(Abs::Top);
                match cond.is_truthy() {
                    Some(true) => succs.push((*target, Frame { stack, vars })),
                    Some(false) => succs.push((pc + 1, Frame { stack, vars })),
                    None => {
                        succs.push((
                            *target,
                            Frame {
                                stack: stack.clone(),
                                vars: vars.clone(),
                            },
                        ));
                        succs.push((pc + 1, Frame { stack, vars }));
                    }
                }
            }
            Instr::JumpIfNotTruthyPeek(target) => {
                let cond = stack.last().copied().unwrap_or(Abs::Top);
                match cond.is_truthy() {
                    Some(false) => succs.push((*target, Frame { stack, vars })),
                    Some(true) => succs.push((pc + 1, Frame { stack, vars })),
                    None => {
                        succs.push((
                            *target,
                            Frame {
                                stack: stack.clone(),
                                vars: vars.clone(),
                            },
                        ));
                        succs.push((pc + 1, Frame { stack, vars }));
                    }
                }
            }
            Instr::JumpIfNotFirstBar(target) => {
                if first_bar {
                    succs.push((pc + 1, Frame { stack, vars }));
                } else {
                    succs.push((*target, Frame { stack, vars }));
                }
            }
            Instr::SetFirstBarFalse => {
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Drop => {
                stack.pop();
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Binop(_) => {
                stack.pop();
                stack.pop();
                // Reachability only: never fold user arithmetic.
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Compare(cmp) => {
                let right = stack.pop().unwrap_or(Abs::Top);
                let left = stack.pop().unwrap_or(Abs::Top);
                stack.push(compare_abs(*cmp, left, right));
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Unary(un) => {
                let operand = stack.pop().unwrap_or(Abs::Top);
                stack.push(match un {
                    super::compile::Unary::Not => match operand.is_truthy() {
                        Some(b) => Abs::Bool(!b),
                        None => Abs::Top,
                    },
                    _ => Abs::Top,
                });
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Call(_, nargs) => {
                for _ in 0..*nargs {
                    stack.pop();
                }
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Average(_) => {
                stack.pop();
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::Crosses { .. } => {
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::BarRefSeries(_) | Instr::BarRefVar(_) | Instr::BarRefPositionKw(_) => {
                stack.pop();
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::DataOf { .. } => {
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::LoadVar(slot) => {
                if *slot < vars.len() {
                    vars[*slot] = hist.get(*slot).copied().unwrap_or(Abs::Top);
                }
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::StoreVar(slot) => {
                let v = stack.pop().unwrap_or(Abs::Top);
                if *slot < vars.len() {
                    vars[*slot] = v;
                    // Self-update inside the Date<>SessionDate then-block is
                    // session-local (e.g. HighGap = 0 - HighGap). A prefix-length
                    // accumulator lives outside that block and must refuse skip.
                    if *slot < program.var_double_accum.len()
                        && program.var_double_accum[*slot]
                        && !session_then.iter().any(|&(lo, hi)| pc >= lo && pc < hi)
                    {
                        double_accum = true;
                    }
                }
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::EmitOrder { .. } => {
                order = true;
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::RiskSet(_) | Instr::Nop => {
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::BinopC { .. }
            | Instr::BinopCLhs { .. }
            | Instr::CompareC { .. }
            | Instr::CompareCLhs { .. } => {
                stack.pop();
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::SeriesCompareC { series, cmp, rhs } => {
                let left = if *series == Series::Date {
                    Abs::DateLe(max_date)
                } else {
                    Abs::Top
                };
                stack.push(compare_abs(*cmp, left, Abs::Num(*rhs)));
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::AverageConst { .. }
            | Instr::BarRefSeriesConst { .. }
            | Instr::BarRefVarConst { .. }
            | Instr::SeriesBinopC { .. }
            | Instr::SeriesBinopCLhs { .. }
            | Instr::VarBinopC { .. }
            | Instr::VarBinopCLhs { .. }
            | Instr::VarCompareC { .. }
            | Instr::VarCompareCLhs { .. } => {
                stack.push(Abs::Top);
                succs.push((pc + 1, Frame { stack, vars }));
            }
            Instr::EmitOrderConst { .. } | Instr::EmitOrderAll { .. } => {
                order = true;
                succs.push((pc + 1, Frame { stack, vars }));
            }
        }
        for (next, nxt) in succs {
            if next > n {
                failed = true;
                continue;
            }
            match &mut at[next] {
                None => {
                    at[next] = Some(nxt);
                    if next <= n {
                        work.push(next);
                    }
                }
                Some(old) => match merge_frame(old, &nxt) {
                    Some(merged) if merged != *old => {
                        *old = merged;
                        work.push(next);
                    }
                    Some(_) => {}
                    None => failed = true,
                },
            }
        }
    }

    let hist_out = at[n]
        .as_ref()
        .map(|f| f.vars.clone())
        .unwrap_or_else(|| hist.to_vec());
    BarAbs {
        order,
        double_accum,
        failed,
        hist: hist_out,
    }
}

fn orders_unreachable_below(program: &Program, cand: &DateLower) -> bool {
    let max_date = if cand.inclusive {
        cand.bound.saturating_sub(1)
    } else {
        cand.bound
    };
    let n_vars = program.var_inits.len();
    let mut hist: Vec<Abs> = program.var_inits.iter().map(from_value).collect();
    if hist.len() != n_vars {
        return false;
    }
    let mut first = true;
    let mut market = Abs::Num(0.0);
    let session_then = session_rollover_then_ranges(program);
    for _ in 0..MAX_BAR_FIXPOINT {
        let bar = interpret_bar(program, &hist, max_date, first, market, &session_then);
        if bar.failed || bar.order {
            return false;
        }
        // A prefix-reachable self-update (`x = x + 1`) would make later bars
        // depend on the skipped length. Refuse rather than reconstruct it.
        if bar.double_accum {
            return false;
        }
        first = false;
        if bar.hist == hist {
            return true;
        }
        hist = bar.hist;
        market = Abs::Num(0.0);
    }
    false
}

#[cfg(test)]
mod tests {
    use super::super::compile::compile;
    use super::*;
    use crate::types::BarTape;
    use serde_json::json;
    use std::collections::HashMap;

    fn ident(name: &str) -> serde_json::Value {
        json!({"type": "ident", "name": name})
    }

    fn num(v: i64) -> serde_json::Value {
        json!({"type": "number", "value": v.to_string()})
    }

    fn cmp(left: serde_json::Value, op: &str, right: serde_json::Value) -> serde_json::Value {
        json!({"type": "compare", "op": op, "left": left, "right": right})
    }

    fn assign(name: &str, value: serde_json::Value) -> serde_json::Value {
        json!({"type": "assign", "target": ident(name), "value": value})
    }

    fn if_then(
        cond: serde_json::Value,
        body: Vec<serde_json::Value>,
        else_body: Vec<serde_json::Value>,
    ) -> serde_json::Value {
        json!({
            "type": "if",
            "cond": cond,
            "body": body,
            "else_body": else_body,
        })
    }

    fn buy() -> serde_json::Value {
        json!({
            "type": "order",
            "action": "buy",
            "order_type": "market",
            "bar_timing": "next",
            "quantity": num(1),
            "label": {"type": "string", "value": "LE"},
            "price": null,
            "entry_label": null,
        })
    }

    fn compile_body(body: Vec<serde_json::Value>) -> Program {
        let ast = json!({"type": "program", "body": body});
        compile(&ast, &HashMap::new()).expect("compile")
    }

    fn tape_dates(dates: &[i64]) -> BarTape {
        let n = dates.len();
        BarTape::from_columns(
            vec![100.0; n],
            vec![101.0; n],
            vec![99.0; n],
            vec![100.5; n],
            vec![1.0; n],
            dates.to_vec(),
            vec![1000; n],
        )
        .unwrap()
    }

    #[test]
    fn ungated_buy_falls_back_loudly() {
        let program = compile_body(vec![buy()]);
        let tape = tape_dates(&[1030101, 1030102, 1180212]);
        let plan = plan(&program, tape.dates());
        assert!(!plan.proven, "{:?}", plan.reason);
        assert_eq!(plan.eval_from, 0);
        assert!(
            plan.reason.contains("cannot prove") || plan.reason.contains("no Date"),
            "{}",
            plan.reason
        );
    }

    /// A study that only draws still has to run. "No orders" is not a
    /// licence to skip the tape.
    #[test]
    fn drawing_call_blocks_the_full_tape_skip() {
        let program = compile_body(vec![
            json!({
                "type": "var_decl",
                "decls": [{
                    "name": "tlid",
                    "init": {"type": "unaryop", "op": "-", "operand": num(1)},
                }],
            }),
            assign(
                "tlid",
                json!({
                    "type": "call",
                    "name": "tl_new",
                    "args": [
                        ident("date"), ident("time"), ident("low"),
                        ident("date"), ident("time"), ident("high"),
                    ],
                }),
            ),
        ]);
        let tape = tape_dates(&[1260102, 1260102]);
        let plan = plan(&program, tape.dates());
        assert!(!plan.proven, "{}", plan.reason);
        assert_eq!(plan.eval_from, 0);
        assert!(plan.reason.contains("drawing"), "{}", plan.reason);
    }

    #[test]
    fn date_gated_buy_skips_prefix() {
        let program = compile_body(vec![
            json!({
                "type": "input_decl",
                "decls": [{
                    "name": "firsttradedate",
                    "default": num(1180212),
                }],
            }),
            if_then(
                cmp(ident("date"), ">=", ident("firsttradedate")),
                vec![buy()],
                vec![],
            ),
        ]);
        // 4 sessions of 2 bars, then the in-range session.
        let mut dates = Vec::new();
        for d in [1030101i64, 1030102, 1030103, 1030104, 1180212] {
            dates.push(d);
            dates.push(d);
        }
        let tape = tape_dates(&dates);
        let plan = plan(&program, tape.dates());
        assert!(plan.proven, "{}", plan.reason);
        assert!(plan.eval_from < dates.len());
        assert!(plan.eval_from > 0, "should skip the 2003 prefix");
        // first in-range is index 8; no session copies → eval_from == 8.
        assert_eq!(plan.eval_from, 8, "{}", plan.reason);
    }

    #[test]
    fn session_copy_warms_two_sessions() {
        let program = compile_body(vec![
            json!({
                "type": "input_decl",
                "decls": [{
                    "name": "firsttradedate",
                    "default": num(1180212),
                }],
            }),
            json!({
                "type": "var_decl",
                "decls": [
                    {"name": "sessiondate", "init": num(0), "data_ref": null},
                    {"name": "sessionclose", "init": num(0), "data_ref": null},
                    {"name": "priorclose", "init": num(0), "data_ref": null},
                    {"name": "twoago", "init": num(0), "data_ref": null},
                    {"name": "ready", "init": {"type": "boolean", "value": false}, "data_ref": null},
                ],
            }),
            if_then(
                cmp(ident("date"), "<>", ident("sessiondate")),
                vec![
                    if_then(
                        cmp(ident("sessiondate"), "<>", num(0)),
                        vec![
                            assign("twoago", ident("priorclose")),
                            assign("priorclose", ident("sessionclose")),
                            if_then(
                                cmp(ident("twoago"), ">", num(0)),
                                vec![assign("ready", json!({"type": "boolean", "value": true}))],
                                vec![],
                            ),
                        ],
                        vec![],
                    ),
                    assign("sessiondate", ident("date")),
                    assign("sessionclose", ident("close")),
                ],
                vec![assign("sessionclose", ident("close"))],
            ),
            if_then(
                json!({
                    "type": "binop",
                    "op": "and",
                    "left": cmp(ident("date"), ">=", ident("firsttradedate")),
                    "right": ident("ready"),
                }),
                vec![buy()],
                vec![],
            ),
        ]);
        let mut dates = Vec::new();
        for d in [1030101i64, 1030102, 1030103, 1030104, 1180212] {
            dates.push(d);
            dates.push(d);
        }
        let tape = tape_dates(&dates);
        let plan = plan(&program, tape.dates());
        assert!(plan.proven, "{}", plan.reason);
        // first in-range at index 8; 2-session warmup of 2 bars each → 4.
        assert_eq!(plan.eval_from, 4, "{}", plan.reason);
        assert!(plan.reason.contains("2-session"), "{}", plan.reason);
    }

    #[test]
    fn once_falls_back() {
        let program = compile_body(vec![json!({
            "type": "once",
            "body": [buy()],
        })]);
        let tape = tape_dates(&[1030101, 1180212]);
        let plan = plan(&program, tape.dates());
        assert!(!plan.proven, "{:?}", plan.reason);
        assert!(plan.reason.contains("Once"), "{}", plan.reason);
    }

    #[test]
    fn no_orders_skips_everything() {
        let program = compile_body(vec![
            json!({
                "type": "var_decl",
                "decls": [{"name": "x", "init": num(0), "data_ref": null}],
            }),
            assign("x", ident("close")),
        ]);
        let tape = tape_dates(&[1030101, 1030102, 1180212]);
        let plan = plan(&program, tape.dates());
        assert!(plan.proven, "{}", plan.reason);
        assert_eq!(plan.eval_from, 3);
    }

    fn divide_program(op: &str, divisor: serde_json::Value) -> Program {
        compile_body(vec![
            json!({
                "type": "var_decl",
                "decls": [{"name": "x", "init": num(0), "data_ref": null}],
            }),
            assign(
                "x",
                json!({"type": "binop", "op": op, "left": ident("close"), "right": divisor}),
            ),
        ])
    }

    #[test]
    fn variable_divisor_blocks_the_skip() {
        // Python raises on a zero divisor, so every bar must be evaluated.
        let tape = tape_dates(&[1030101, 1030102, 1180212]);
        for op in ["/", "%"] {
            let plan = plan(&divide_program(op, ident("x")), tape.dates());
            assert!(!plan.proven, "{op}: {}", plan.reason);
            assert_eq!(plan.eval_from, 0);
        }
    }

    #[test]
    fn non_zero_literal_divisor_still_skips() {
        let tape = tape_dates(&[1030101, 1030102, 1180212]);
        for op in ["/", "%"] {
            let plan = plan(&divide_program(op, num(4)), tape.dates());
            assert!(plan.proven, "{op}: {}", plan.reason);
            assert_eq!(plan.eval_from, 3);
        }
    }

    #[test]
    fn session_skip_fills_match_full_eval() {
        let body = vec![
            json!({
                "type": "input_decl",
                "decls": [{
                    "name": "firsttradedate",
                    "default": num(1180212),
                }],
            }),
            json!({
                "type": "var_decl",
                "decls": [
                    {"name": "sessiondate", "init": num(0), "data_ref": null},
                    {"name": "sessionclose", "init": num(0), "data_ref": null},
                    {"name": "priorclose", "init": num(0), "data_ref": null},
                    {"name": "twoago", "init": num(0), "data_ref": null},
                    {"name": "ready", "init": {"type": "boolean", "value": false}, "data_ref": null},
                ],
            }),
            if_then(
                cmp(ident("date"), "<>", ident("sessiondate")),
                vec![
                    if_then(
                        cmp(ident("sessiondate"), "<>", num(0)),
                        vec![
                            assign("twoago", ident("priorclose")),
                            assign("priorclose", ident("sessionclose")),
                            if_then(
                                cmp(ident("twoago"), ">", num(0)),
                                vec![assign("ready", json!({"type": "boolean", "value": true}))],
                                vec![],
                            ),
                        ],
                        vec![],
                    ),
                    assign("sessiondate", ident("date")),
                    assign("sessionclose", ident("close")),
                ],
                vec![assign("sessionclose", ident("close"))],
            ),
            if_then(
                json!({
                    "type": "binop",
                    "op": "and",
                    "left": cmp(ident("date"), ">=", ident("firsttradedate")),
                    "right": ident("ready"),
                }),
                vec![buy()],
                vec![],
            ),
        ];
        let ast = json!({"type": "program", "body": body});
        let mut dates = Vec::new();
        for d in [1030101i64, 1030102, 1030103, 1030104, 1180212, 1180213] {
            dates.push(d);
            dates.push(d);
        }
        let tape = tape_dates(&dates);
        let mut skipped_strat =
            super::super::interp::Interpreter::new(ast.clone(), &HashMap::new(), None).unwrap();
        let mut full_strat =
            super::super::interp::Interpreter::new(ast, &HashMap::new(), None).unwrap();
        let instrument = crate::types::Instrument::default();
        let skipped = crate::kernel::run_native(&tape, instrument, &mut skipped_strat).unwrap();
        let full = crate::kernel::run_native_from(&tape, instrument, &mut full_strat, 0).unwrap();
        assert!(skipped.skipped_bars > 0, "expected a prefix skip");
        assert_eq!(
            skipped.fills.len(),
            full.fills.len(),
            "skip changed fill count: skipped={} full={}",
            skipped.fills.len(),
            full.fills.len()
        );
        for (i, (a, b)) in skipped.fills.iter().zip(full.fills.iter()).enumerate() {
            assert_eq!(a, b, "fill {i} diverged under skip");
        }
    }

    #[test]
    #[ignore]
    fn audit_ref3_ir_proves_prefix_skip() {
        let path = std::env::var("AUDIT_REF3_IR")
            .expect("set AUDIT_REF3_IR to the serialised reference-strategy IR");
        let payload = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let ast = super::super::ir::load_program_ir(&payload).expect("load IR");
        let program = compile(&ast, &HashMap::new()).expect("compile reference strategy");
        let mut dates = Vec::new();
        for d in [
            1030101i64, 1100101, 1171231, 1180101, 1180211, 1180212, 1180213,
        ] {
            for _ in 0..3 {
                dates.push(d);
            }
        }
        let tape = tape_dates(&dates);
        let plan = plan(&program, tape.dates());
        assert!(
            plan.proven,
            "reference strategy must prove a skip, got: {}",
            plan.reason
        );
        assert!(plan.eval_from > 0, "{}", plan.reason);
        assert!(
            plan.reason.contains("1180212"),
            "expected FirstTradeDate bound, got {}",
            plan.reason
        );
        assert!(
            plan.reason.contains("2-session"),
            "expected 2-session warmup, got {}",
            plan.reason
        );
        eprintln!(
            "reference strategy plan: eval_from={} reason={}",
            plan.eval_from, plan.reason
        );
    }
}
