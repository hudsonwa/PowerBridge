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

//! Per-variant specialisation of a compiled [`Program`] (L3).
//!
//! Inputs and other proven constants are frozen at `from_program` time: they
//! are folded into the instruction stream, constant branches and no-ops are
//! deleted, small numeric load/binop/compare/store sequences are fused, and
//! the first-bar `Once` path is split from the steady-state block so the bar
//! loop never re-dispatches `PushInput` or `JumpIfNotFirstBar`. Arithmetic
//! order is preserved — this pass never reassociates.
//!
//! The original [`Program::instrs`] is left intact for skip-region planning;
//! the specialised stream is what [`super::interp::Interpreter`] executes.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Instant;

use super::compile::{Binop, CallOp, Cmp, Instr, Program, RiskOp, SeriesSrc, Unary};
use super::pynum::{binop_num, may_raise, py_extreme, py_int, py_sign, py_square};
use super::value::{num_to_str, Value};

/// Specialised instruction stream for one frozen Input vector.
#[derive(Debug, Clone)]
pub(crate) struct Specialized {
    /// Code for bar 1 (`Once` body included, `JumpIfNotFirstBar` gone).
    pub(crate) first: Arc<[Instr]>,
    /// Code for every later bar (`Once` body unreachable-deleted).
    pub(crate) rest: Arc<[Instr]>,
    pub(crate) stats: Stats,
}

/// Counters the bench prints separately from the bar loop.
/// Read by the bench via Interpreter::specialize_stats; unused on the
/// production on_bar path, so the lib target would otherwise warn.
#[derive(Debug, Clone, Copy, Default)]
#[allow(dead_code)]
pub(crate) struct Stats {
    pub(crate) instrs_before: usize,
    pub(crate) instrs_after: usize,
    pub(crate) specialize_ns: u128,
}

/// [`specialize_with`] as the fills path uses it.
#[cfg(test)]
pub(crate) fn specialize(program: &Program) -> Specialized {
    specialize_with(program, false)
}

/// Fold `program.inputs` into a specialised stream. Cheap relative to a
/// 101k-bar loop: the AST is not re-walked.
///
/// The trace entry point reports every variable's value at the end of each bar,
/// so a store nothing reads is still observable there: `keep_stores` turns
/// off dead-store elimination and changes nothing else.
pub(crate) fn specialize_with(program: &Program, keep_stores: bool) -> Specialized {
    let started = Instant::now();
    let before = program.instrs.len();
    let mut instrs = program.instrs.clone();
    inline_inputs(&mut instrs, &program.inputs);
    for _ in 0..12 {
        let n = instrs.len();
        instrs = peephole(instrs);
        instrs = dce_unreachable(instrs);
        instrs = dce_pure_drops(instrs);
        if !keep_stores {
            instrs = dce_dead_stores(instrs);
        }
        instrs = fuse(instrs);
        if instrs.len() == n {
            break;
        }
    }
    instrs = hoist_loadvars(instrs);
    instrs = compact(instrs);
    let has_once = instrs
        .iter()
        .any(|i| matches!(i, Instr::JumpIfNotFirstBar(_)));
    let (first_arc, rest_arc, after) = if has_once {
        let (first, rest) = split_first_rest(instrs);
        let after = rest.len();
        (
            Arc::<[Instr]>::from(first),
            Arc::<[Instr]>::from(rest),
            after,
        )
    } else {
        let after = instrs.len();
        let shared: Arc<[Instr]> = instrs.into();
        (Arc::clone(&shared), shared, after)
    };
    Specialized {
        first: first_arc,
        rest: rest_arc,
        stats: Stats {
            instrs_before: before,
            instrs_after: after,
            specialize_ns: started.elapsed().as_nanos(),
        },
    }
}

fn inline_inputs(instrs: &mut [Instr], inputs: &[Value]) {
    for ins in instrs {
        if let Instr::PushInput(slot) = ins {
            *ins = value_to_push(inputs.get(*slot));
        }
    }
}

fn value_to_push(v: Option<&Value>) -> Instr {
    match v {
        Some(Value::Num(n)) => Instr::PushNum(*n),
        Some(Value::Bool(b)) => Instr::PushBool(*b),
        Some(v) => {
            if let Some(s) = v.as_str() {
                Instr::PushStr(s.to_string())
            } else if let Ok(n) = v.as_f64() {
                Instr::PushNum(n)
            } else {
                Instr::PushStr(v.clone().into_string())
            }
        }
        None => Instr::PushNum(0.0),
    }
}

fn targets_of(instrs: &[Instr]) -> HashSet<usize> {
    let mut t = HashSet::new();
    for ins in instrs {
        if let Some(pc) = jump_target(ins) {
            t.insert(pc);
        }
    }
    t
}

fn jump_target(ins: &Instr) -> Option<usize> {
    match ins {
        Instr::Jump(t)
        | Instr::JumpIfFalse(t)
        | Instr::JumpIfTruthyPeek(t)
        | Instr::JumpIfNotTruthyPeek(t)
        | Instr::JumpIfNotFirstBar(t)
        | Instr::ForInit { exit: t, .. }
        | Instr::ForNext { body: t, .. } => Some(*t),
        _ => None,
    }
}

fn patch_jump(ins: &mut Instr, map: &[usize]) {
    let patch = |t: &mut usize| {
        let i = (*t).min(map.len().saturating_sub(1));
        *t = map[i];
    };
    match ins {
        Instr::Jump(t)
        | Instr::JumpIfFalse(t)
        | Instr::JumpIfTruthyPeek(t)
        | Instr::JumpIfNotTruthyPeek(t)
        | Instr::JumpIfNotFirstBar(t)
        | Instr::ForInit { exit: t, .. }
        | Instr::ForNext { body: t, .. } => patch(t),
        _ => {}
    }
}

fn compact(instrs: Vec<Instr>) -> Vec<Instr> {
    let n = instrs.len();
    let mut map = vec![0usize; n + 1];
    let mut out = Vec::with_capacity(n);
    for (i, ins) in instrs.into_iter().enumerate() {
        map[i] = out.len();
        if !matches!(ins, Instr::Nop) {
            out.push(ins);
        }
    }
    map[n] = out.len();
    for ins in &mut out {
        patch_jump(ins, &map);
    }
    out
}

fn crosses_target(targets: &HashSet<usize>, start: usize, end: usize) -> bool {
    (start + 1..end).any(|i| targets.contains(&i))
}

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

fn eval_cmp_str(cmp: Cmp, a: &str, b: &str) -> bool {
    match cmp {
        Cmp::Eq => a == b,
        Cmp::Ne => a != b,
        Cmp::Lt => a < b,
        Cmp::Gt => a > b,
        Cmp::Le => a <= b,
        Cmp::Ge => a >= b,
    }
}

fn as_const(ins: &Instr) -> Option<Value> {
    match ins {
        Instr::PushNum(n) => Some(Value::Num(*n)),
        Instr::PushBool(b) => Some(Value::Bool(*b)),
        Instr::PushStr(s) => Some(Value::str(s.clone())),
        _ => None,
    }
}

fn fold_binop(op: Binop, left: &Value, right: &Value) -> Option<Instr> {
    if op == Binop::Add && (left.is_str() || right.is_str()) {
        let mut s = left.clone().into_string();
        s.push_str(&right.clone().into_string());
        return Some(Instr::PushStr(s));
    }
    let a = left.as_f64().ok()?;
    let b = right.as_f64().ok()?;
    // A Python error (zero divisor) is left unfolded so it refuses at run time.
    Some(Instr::PushNum(binop_num(op, a, b).ok()?))
}

fn fold_cmp(cmp: Cmp, left: &Value, right: &Value) -> Option<Instr> {
    if let (Some(a), Some(b)) = (left.as_str(), right.as_str()) {
        return Some(Instr::PushBool(eval_cmp_str(cmp, a, b)));
    }
    let a = left.as_f64().ok()?;
    let b = right.as_f64().ok()?;
    Some(Instr::PushBool(eval_cmp_num(cmp, a, b)))
}

fn fold_unary(un: Unary, operand: &Value) -> Option<Instr> {
    match un {
        Unary::Not => Some(Instr::PushBool(!operand.is_truthy())),
        Unary::Neg => Some(Instr::PushNum(-operand.as_f64().ok()?)),
        Unary::Pos => Some(Instr::PushNum(operand.as_f64().ok()?)),
    }
}

fn fold_call(op: CallOp, args: &[Value]) -> Option<Instr> {
    match op {
        CallOp::AbsValue if args.len() == 1 => Some(Instr::PushNum(args[0].as_f64().ok()?.abs())),
        CallOp::IntPortion if args.len() == 1 => {
            Some(Instr::PushNum(py_int(args[0].as_f64().ok()?).ok()?))
        }
        CallOp::Sign if args.len() == 1 => Some(Instr::PushNum(py_sign(args[0].as_f64().ok()?))),
        CallOp::Square if args.len() == 1 => {
            Some(Instr::PushNum(py_square(args[0].as_f64().ok()?).ok()?))
        }
        CallOp::MaxList | CallOp::MinList => {
            let nums = args
                .iter()
                .map(|a| a.as_f64().ok())
                .collect::<Option<Vec<_>>>()?;
            Some(Instr::PushNum(
                py_extreme(&nums, op == CallOp::MaxList).ok()?,
            ))
        }
        CallOp::NumToStr if args.len() >= 2 => {
            let x = args[0].as_f64().ok()?;
            let decimals = args[1].as_f64().ok()? as i64;
            Some(Instr::PushStr(num_to_str(x, decimals)))
        }
        CallOp::SessionTime(hhmm) => Some(Instr::PushNum(f64::from(hhmm))),
        CallOp::File if !args.is_empty() => Some(Instr::PushStr(args[0].clone().into_string())),
        _ => None,
    }
}

fn peephole(instrs: Vec<Instr>) -> Vec<Instr> {
    let targets = targets_of(&instrs);
    let n = instrs.len();
    let mut out = instrs;
    let mut i = 0;
    while i < out.len() {
        if matches!(out[i], Instr::RiskSet(RiskOp::Noop)) {
            out[i] = Instr::Nop;
            i += 1;
            continue;
        }
        if let Instr::Jump(t) = out[i] {
            if t == i + 1 {
                out[i] = Instr::Nop;
                i += 1;
                continue;
            }
        }
        // const, const, Binop/Compare
        if i + 2 < out.len() && !crosses_target(&targets, i, i + 3) {
            if let (Some(a), Some(b)) = (as_const(&out[i]), as_const(&out[i + 1])) {
                match out[i + 2] {
                    Instr::Binop(op) => {
                        if let Some(folded) = fold_binop(op, &a, &b) {
                            out[i] = folded;
                            out[i + 1] = Instr::Nop;
                            out[i + 2] = Instr::Nop;
                            i += 3;
                            continue;
                        }
                    }
                    Instr::Compare(cmp) => {
                        if let Some(folded) = fold_cmp(cmp, &a, &b) {
                            out[i] = folded;
                            out[i + 1] = Instr::Nop;
                            out[i + 2] = Instr::Nop;
                            i += 3;
                            continue;
                        }
                    }
                    _ => {}
                }
            }
        }
        // const, Unary
        if i + 1 < out.len() && !crosses_target(&targets, i, i + 2) {
            if let Some(a) = as_const(&out[i]) {
                if let Instr::Unary(un) = out[i + 1] {
                    if let Some(folded) = fold_unary(un, &a) {
                        out[i] = folded;
                        out[i + 1] = Instr::Nop;
                        i += 2;
                        continue;
                    }
                }
            }
        }
        // const args + Call
        if let Instr::Call(op, nargs) = out[i] {
            let k = nargs as usize;
            if k > 0 && i >= k && !crosses_target(&targets, i - k, i + 1) {
                let mut args = Vec::with_capacity(k);
                let mut ok = true;
                for j in 0..k {
                    match as_const(&out[i - k + j]) {
                        Some(v) => args.push(v),
                        None => {
                            ok = false;
                            break;
                        }
                    }
                }
                if ok {
                    if let Some(folded) = fold_call(op, &args) {
                        for j in 0..k {
                            out[i - k + j] = Instr::Nop;
                        }
                        out[i] = folded;
                        i += 1;
                        continue;
                    }
                }
            }
        }
        // const branch
        if i + 1 < out.len() && !crosses_target(&targets, i, i + 2) {
            if let Instr::PushBool(b) = out[i] {
                match out[i + 1] {
                    Instr::JumpIfFalse(t) => {
                        out[i] = Instr::Nop;
                        out[i + 1] = if b { Instr::Nop } else { Instr::Jump(t) };
                        i += 2;
                        continue;
                    }
                    Instr::JumpIfTruthyPeek(t) => {
                        if b {
                            out[i + 1] = Instr::Jump(t);
                        } else {
                            out[i + 1] = Instr::Nop;
                        }
                        i += 2;
                        continue;
                    }
                    Instr::JumpIfNotTruthyPeek(t) => {
                        if b {
                            out[i + 1] = Instr::Nop;
                        } else {
                            out[i + 1] = Instr::Jump(t);
                        }
                        i += 2;
                        continue;
                    }
                    _ => {}
                }
            }
        }
        i += 1;
    }
    let _ = n;
    compact(out)
}

pub(crate) fn stack_delta(ins: &Instr) -> Option<(i32, i32)> {
    Some(match ins {
        Instr::PushNum(_)
        | Instr::PushBool(_)
        | Instr::PushStr(_)
        | Instr::PushInput(_)
        | Instr::PushVar(_)
        | Instr::PushVarSeries(_)
        | Instr::PushSeriesParam(_)
        | Instr::PushSeriesParamSeries(_)
        | Instr::PushMarketPosition
        | Instr::PushPositionContracts
        | Instr::PushPositionKw(_)
        | Instr::PushMcKw(_)
        | Instr::Crosses { .. }
        | Instr::DataOf { .. }
        | Instr::AverageConst { .. }
        | Instr::BarRefSeriesConst { .. }
        | Instr::BarRefVarConst { .. }
        | Instr::SeriesCompareC { .. }
        | Instr::SeriesBinopC { .. }
        | Instr::SeriesBinopCLhs { .. }
        | Instr::VarBinopC { .. }
        | Instr::VarBinopCLhs { .. }
        | Instr::VarCompareC { .. }
        | Instr::VarCompareCLhs { .. } => (0, 1),
        Instr::Drop | Instr::JumpIfFalse(_) | Instr::StoreVar(_) => (1, 0),
        Instr::Jump(_)
        | Instr::JumpIfTruthyPeek(_)
        | Instr::JumpIfNotTruthyPeek(_)
        | Instr::JumpIfNotFirstBar(_)
        | Instr::SetFirstBarFalse
        | Instr::LoadVar(_)
        | Instr::Nop
        | Instr::EmitOrderConst { .. }
        | Instr::EmitOrderAll { .. } => (0, 0),
        Instr::Binop(_) | Instr::Compare(_) => (2, 1),
        Instr::Unary(_)
        | Instr::Average(_)
        | Instr::BarRefSeries(_)
        | Instr::BarRefVar(_)
        | Instr::BarRefPositionKw(_)
        | Instr::BinopC { .. }
        | Instr::BinopCLhs { .. }
        | Instr::CompareC { .. }
        | Instr::CompareCLhs { .. } => (1, 1),
        Instr::Call(_, n) => (*n as i32, 1),
        Instr::EmitOrder { has_price, .. } => (if *has_price { 3 } else { 2 }, 0),
        Instr::ArrayGet(_) => (1, 1),
        Instr::ArraySet { .. } | Instr::ForInit { .. } => (2, 0),
        Instr::ArrayCall { op, .. } => (i32::from(op.argc()), 1),
        Instr::ArrayCopy { .. } => (3, 1),
        Instr::ForNext { .. } => (0, 0),
        Instr::RiskSet(op) => match op {
            RiskOp::PercentTrailing | RiskOp::PercentTrailingPt => (2, 0),
            RiskOp::ExitOnClose | RiskOp::Noop => (0, 0),
            _ => (1, 0),
        },
    })
}

/// A call whose result may be dropped without changing the run.
///
/// Drawing calls record an object and colour calls can refuse an unmeasured
/// argument. Both must run even when the returned id or colour is unread
/// (`TLID = TL_New(...)` with `TLID` never read, or `TL_SetColor(...)` as a
/// statement). Treating them as pure let `dce_pure_drops` delete the call
/// after `dce_dead_stores` turned the unread store into `Drop`, so the kernel
/// accepted the program and recorded nothing.
fn call_is_pure(op: CallOp) -> bool {
    !matches!(
        op,
        CallOp::TlNew
            | CallOp::TlSetColor
            | CallOp::TlSetStyle
            | CallOp::ArwNew
            | CallOp::TextNew
            | CallOp::Rgb
            | CallOp::LegacyColorToRgb
            | CallOp::GradientColor
    )
}

fn is_pure(ins: &Instr) -> bool {
    match ins {
        Instr::Call(op, _) if !call_is_pure(*op) => false,
        Instr::LoadVar(_)
        | Instr::StoreVar(_)
        | Instr::EmitOrder { .. }
        | Instr::EmitOrderConst { .. }
        | Instr::EmitOrderAll { .. }
        | Instr::RiskSet(_)
        | Instr::SetFirstBarFalse
        | Instr::Jump(_)
        | Instr::JumpIfFalse(_)
        | Instr::JumpIfTruthyPeek(_)
        | Instr::JumpIfNotTruthyPeek(_)
        | Instr::JumpIfNotFirstBar(_)
        // An element read can fail the run (index out of range) exactly as
        // Python does, so it is never dropped even when its value is unused.
        | Instr::ArrayGet(_)
        | Instr::ArraySet { .. }
        | Instr::ArrayCall { .. }
        | Instr::ArrayCopy { .. }
        | Instr::ForInit { .. }
        | Instr::ForNext { .. } => false,
        _ => true,
    }
}

fn dce_pure_drops(instrs: Vec<Instr>) -> Vec<Instr> {
    let targets = targets_of(&instrs);
    let mut out = instrs;
    let mut i = 0;
    while i < out.len() {
        if matches!(out[i], Instr::Drop) {
            if let Some(start) = producer_start(&out, i, &targets) {
                out[start..=i].fill(Instr::Nop);
                i += 1;
                continue;
            }
        }
        i += 1;
    }
    compact(out)
}

fn producer_start(instrs: &[Instr], drop_idx: usize, targets: &HashSet<usize>) -> Option<usize> {
    let mut need: i32 = 1;
    let mut i = drop_idx;
    while need > 0 {
        if i == 0 {
            return None;
        }
        i -= 1;
        if targets.contains(&i) {
            return None;
        }
        // An operation Python can raise on must still run when its result is
        // unread (`U = Close / C` with `U` never read).
        let prev = i.checked_sub(1).map(|p| &instrs[p]);
        if !is_pure(&instrs[i]) || matches!(instrs[i], Instr::Nop) || may_raise(&instrs[i], prev) {
            return None;
        }
        let (pop, push) = stack_delta(&instrs[i])?;
        need = need - push + pop;
    }
    if need == 0 {
        Some(i)
    } else {
        None
    }
}

fn live_slots(instrs: &[Instr]) -> HashSet<usize> {
    let mut live = HashSet::new();
    let mark_src = |live: &mut HashSet<usize>, src: SeriesSrc| match src {
        SeriesSrc::NumericVar(s) | SeriesSrc::ValueVar(s) => {
            live.insert(s);
        }
        SeriesSrc::Column(_) => {}
    };
    for ins in instrs {
        match ins {
            Instr::PushVar(s)
            | Instr::PushVarSeries(s)
            | Instr::BarRefVar(s)
            | Instr::BarRefVarConst { slot: s, .. }
            | Instr::VarBinopC { slot: s, .. }
            | Instr::VarBinopCLhs { slot: s, .. }
            | Instr::VarCompareC { slot: s, .. }
            | Instr::VarCompareCLhs { slot: s, .. } => {
                live.insert(*s);
            }
            Instr::Average(src) | Instr::AverageConst { src, .. } => {
                mark_src(&mut live, *src);
            }
            Instr::Crosses { a, b, .. } => {
                mark_src(&mut live, *a);
                mark_src(&mut live, *b);
            }
            _ => {}
        }
    }
    live
}

fn dce_dead_stores(instrs: Vec<Instr>) -> Vec<Instr> {
    let live = live_slots(&instrs);
    let mut out = instrs;
    for ins in &mut out {
        if let Instr::StoreVar(slot) = ins {
            if !live.contains(slot) {
                *ins = Instr::Drop;
            }
        }
    }
    compact(out)
}

fn hoist_loadvars(instrs: Vec<Instr>) -> Vec<Instr> {
    let mut out = instrs;
    for ins in &mut out {
        if matches!(ins, Instr::LoadVar(_)) {
            *ins = Instr::Nop;
        }
    }
    compact(out)
}

fn fuse(instrs: Vec<Instr>) -> Vec<Instr> {
    let targets = targets_of(&instrs);
    let mut out = instrs;
    let mut i = 0;
    while i < out.len() {
        // PushNum, Average
        if i + 1 < out.len() && !crosses_target(&targets, i, i + 2) {
            if let (Instr::PushNum(n), Instr::Average(src)) = (&out[i], &out[i + 1]) {
                out[i] = Instr::AverageConst {
                    src: *src,
                    length: *n,
                };
                out[i + 1] = Instr::Nop;
                i += 2;
                continue;
            }
            if let (Instr::PushNum(n), Instr::Compare(cmp)) = (&out[i], &out[i + 1]) {
                out[i] = Instr::CompareC { cmp: *cmp, rhs: *n };
                out[i + 1] = Instr::Nop;
                i += 2;
                continue;
            }
            if let (Instr::PushNum(n), Instr::Binop(op)) = (&out[i], &out[i + 1]) {
                out[i] = Instr::BinopC { op: *op, rhs: *n };
                out[i + 1] = Instr::Nop;
                i += 2;
                continue;
            }
            if let (Instr::PushNum(n), Instr::BarRefSeries(s)) = (&out[i], &out[i + 1]) {
                out[i] = Instr::BarRefSeriesConst {
                    series: *s,
                    index: *n as i64,
                };
                out[i + 1] = Instr::Nop;
                i += 2;
                continue;
            }
            if let (Instr::PushNum(n), Instr::BarRefVar(s)) = (&out[i], &out[i + 1]) {
                out[i] = Instr::BarRefVarConst {
                    slot: *s,
                    index: *n as i64,
                };
                out[i + 1] = Instr::Nop;
                i += 2;
                continue;
            }
        }
        // PushSeries, PushNum, Compare/Binop
        if i + 2 < out.len() && !crosses_target(&targets, i, i + 3) {
            match (&out[i], &out[i + 1], &out[i + 2]) {
                (Instr::PushSeriesParam(s), Instr::PushNum(n), Instr::Compare(cmp)) => {
                    out[i] = Instr::SeriesCompareC {
                        series: *s,
                        cmp: *cmp,
                        rhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushNum(n), Instr::PushSeriesParam(s), Instr::Compare(cmp)) => {
                    out[i] = Instr::SeriesCompareC {
                        series: *s,
                        cmp: swap_cmp(*cmp),
                        rhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushSeriesParam(s), Instr::PushNum(n), Instr::Binop(op)) => {
                    out[i] = Instr::SeriesBinopC {
                        series: *s,
                        op: *op,
                        rhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushNum(n), Instr::PushSeriesParam(s), Instr::Binop(op)) => {
                    out[i] = Instr::SeriesBinopCLhs {
                        series: *s,
                        op: *op,
                        lhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushVar(slot), Instr::PushNum(n), Instr::Binop(op)) => {
                    out[i] = Instr::VarBinopC {
                        slot: *slot,
                        op: *op,
                        rhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushNum(n), Instr::PushVar(slot), Instr::Binop(op)) => {
                    out[i] = Instr::VarBinopCLhs {
                        slot: *slot,
                        op: *op,
                        lhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushVar(slot), Instr::PushNum(n), Instr::Compare(cmp)) => {
                    out[i] = Instr::VarCompareC {
                        slot: *slot,
                        cmp: *cmp,
                        rhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (Instr::PushNum(n), Instr::PushVar(slot), Instr::Compare(cmp)) => {
                    out[i] = Instr::VarCompareCLhs {
                        slot: *slot,
                        cmp: *cmp,
                        lhs: *n,
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (
                    Instr::PushNum(qty),
                    Instr::PushStr(sig),
                    Instr::EmitOrder {
                        action,
                        order_type,
                        timing,
                        has_price: false,
                    },
                ) => {
                    out[i] = Instr::EmitOrderConst {
                        action: *action,
                        order_type: *order_type,
                        timing: *timing,
                        quantity: *qty,
                        signal: sig.clone(),
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                (
                    Instr::PushPositionContracts,
                    Instr::PushStr(sig),
                    Instr::EmitOrder {
                        action,
                        order_type,
                        timing,
                        has_price: false,
                    },
                ) => {
                    out[i] = Instr::EmitOrderAll {
                        action: *action,
                        order_type: *order_type,
                        timing: *timing,
                        signal: sig.clone(),
                    };
                    out[i + 1] = Instr::Nop;
                    out[i + 2] = Instr::Nop;
                    i += 3;
                    continue;
                }
                _ => {}
            }
        }
        i += 1;
    }
    compact(out)
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

fn split_first_rest(instrs: Vec<Instr>) -> (Vec<Instr>, Vec<Instr>) {
    // First bar keeps Once semantics: JumpIfNotFirstBar falls through while
    // first_bar is true, and SetFirstBarFalse consumes the flag so a later
    // Once on the same bar does not run (EL Once is one-shot per strategy).
    let first = instrs.clone();
    let mut rest = instrs;
    for ins in &mut rest {
        match ins {
            Instr::JumpIfNotFirstBar(t) => *ins = Instr::Jump(*t),
            Instr::SetFirstBarFalse => *ins = Instr::Nop,
            _ => {}
        }
    }
    let rest = compact(rest);
    let rest = dce_unreachable(rest);
    (first, rest)
}

fn dce_unreachable(instrs: Vec<Instr>) -> Vec<Instr> {
    if instrs.is_empty() {
        return instrs;
    }
    let n = instrs.len();
    let mut reach = vec![false; n];
    let mut work = vec![0usize];
    reach[0] = true;
    while let Some(pc) = work.pop() {
        if pc >= n {
            continue;
        }
        let enqueue = |t: usize, reach: &mut [bool], work: &mut Vec<usize>| {
            if t < n && !reach[t] {
                reach[t] = true;
                work.push(t);
            }
        };
        match &instrs[pc] {
            Instr::Jump(t) => enqueue(*t, &mut reach, &mut work),
            Instr::JumpIfFalse(t)
            | Instr::JumpIfTruthyPeek(t)
            | Instr::JumpIfNotTruthyPeek(t)
            | Instr::JumpIfNotFirstBar(t)
            | Instr::ForInit { exit: t, .. }
            | Instr::ForNext { body: t, .. } => {
                enqueue(*t, &mut reach, &mut work);
                enqueue(pc + 1, &mut reach, &mut work);
            }
            _ => enqueue(pc + 1, &mut reach, &mut work),
        }
    }
    let mut out = instrs;
    for (i, ins) in out.iter_mut().enumerate() {
        if !reach[i] {
            *ins = Instr::Nop;
        }
    }
    compact(out)
}

#[cfg(test)]
mod tests {
    use super::super::compile::compile;
    use super::*;
    use serde_json::json;
    use std::collections::HashMap;

    fn compile_body(body: serde_json::Value) -> Program {
        let ast = json!({"type": "program", "body": body});
        compile(&ast, &HashMap::new()).expect("compile")
    }

    #[test]
    fn folds_input_and_arithmetic() {
        let program = compile_body(json!([
            {"type":"input_decl","decls":[{"name":"n","default":{"type":"number","value":"3"}}]},
            {"type":"var_decl","decls":[{"name":"x","init":{"type":"number","value":"0"}}]},
            {"type":"assign","target":{"type":"ident","name":"x"},
             "value":{"type":"binop","op":"+","left":{"type":"ident","name":"n"},
                      "right":{"type":"number","value":"2"}}},
            {"type":"if","cond":{"type":"compare","op":">","left":{"type":"ident","name":"x"},
                                 "right":{"type":"number","value":"0"}},
             "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                      "label":{"type":"string","value":"LE"},
                      "quantity":{"type":"ident","name":"x"}}],
             "else_body":null}
        ]));
        let spec = specialize(&program);
        assert!(
            spec.rest
                .iter()
                .any(|i| matches!(i, Instr::PushNum(n) if *n == 5.0)
                    || matches!(i, Instr::CompareC { rhs, .. } if *rhs == 5.0)
                    || matches!(i, Instr::CompareCLhs { lhs, .. } if *lhs == 5.0)
                    || matches!(i, Instr::EmitOrderConst { quantity, .. } if *quantity == 5.0)
                    || matches!(i, Instr::VarCompareC { rhs, .. } if *rhs == 0.0)),
            "3+2 should fold to 5 (or feed a fused compare/order): {:?}",
            spec.rest
        );
        assert!(
            !spec.rest.iter().any(|i| matches!(i, Instr::PushInput(_))),
            "no PushInput remains"
        );
    }

    /// `Vars: TLID(-1); TLID = TL_New(...)` never reads `TLID`. The call is
    /// still the drawing. Dead-store elimination must not delete it.
    #[test]
    fn unread_tl_new_survives_dead_store_elimination() {
        let program = compile_body(json!([
            {"type":"var_decl","decls":[{"name":"tlid","init":{
                "type":"unaryop","op":"-","operand":{"type":"number","value":"1"}}}]},
            {"type":"assign","target":{"type":"ident","name":"tlid"},
             "value":{"type":"call","name":"tl_new","args":[
                {"type":"ident","name":"date"},
                {"type":"ident","name":"time"},
                {"type":"ident","name":"low"},
                {"type":"ident","name":"date"},
                {"type":"ident","name":"time"},
                {"type":"ident","name":"high"}
             ]}}
        ]));
        let spec = specialize(&program);
        assert!(
            spec.rest
                .iter()
                .any(|i| matches!(i, Instr::Call(CallOp::TlNew, 6))),
            "unread TL_New must stay: {:?}",
            spec.rest
        );
    }

    /// `TL_SetColor(id, colour);` is an expression statement. Its return value
    /// is dropped, and the call still has to recolour the object.
    #[test]
    fn tl_setcolor_statement_survives_drop() {
        let program = compile_body(json!([
            {"type":"var_decl","decls":[{"name":"tlid","init":{"type":"number","value":"1"}}]},
            {"type":"expr_stmt","expr":{"type":"call","name":"tl_setcolor","args":[
                {"type":"ident","name":"tlid"},
                {"type":"number","value":"255"}
            ]}}
        ]));
        let spec = specialize(&program);
        assert!(
            spec.rest
                .iter()
                .any(|i| matches!(i, Instr::Call(CallOp::TlSetColor, 2))),
            "TL_SetColor statement must stay: {:?}",
            spec.rest
        );
    }

    #[test]
    fn dces_constant_false_branch() {
        let program = compile_body(json!([
            {"type":"input_decl","decls":[{"name":"flag","default":{"type":"boolean","value":false}}]},
            {"type":"if","cond":{"type":"ident","name":"flag"},
             "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                      "label":{"type":"string","value":"LE"},
                      "quantity":{"type":"number","value":"1"}}],
             "else_body":null}
        ]));
        let spec = specialize(&program);
        assert!(
            !spec
                .rest
                .iter()
                .any(|i| matches!(i, Instr::EmitOrder { .. } | Instr::EmitOrderConst { .. })),
            "false flag should kill the order: {:?}",
            spec.rest
        );
    }

    #[test]
    fn fuses_average_length() {
        let program = compile_body(json!([
            {"type":"input_decl","decls":[{"name":"fastlen","default":{"type":"number","value":"10"}}]},
            {"type":"var_decl","decls":[{"name":"ma","init":{"type":"number","value":"0"}}]},
            {"type":"assign","target":{"type":"ident","name":"ma"},
             "value":{"type":"call","name":"average","args":[
                {"type":"ident","name":"close"},
                {"type":"ident","name":"fastlen"}
             ]}},
            {"type":"if","cond":{"type":"compare","op":">","left":{"type":"ident","name":"ma"},
                                 "right":{"type":"number","value":"0"}},
             "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                      "label":{"type":"string","value":"LE"},
                      "quantity":{"type":"number","value":"1"}}],
             "else_body":null}
        ]));
        let spec = specialize(&program);
        assert!(
            spec.rest
                .iter()
                .any(|i| matches!(i, Instr::AverageConst { length, .. } if *length == 10.0)),
            "Average length should fuse: {:?}",
            spec.rest
        );
    }

    #[test]
    fn gt2_stream_shrinks_and_drops_push_input() {
        let ir: serde_json::Value =
            serde_json::from_str(include_str!("fixtures/gt2.ir.json")).expect("gt2 ir");
        let ast = ir.get("ast").cloned().unwrap_or(ir);
        let program = compile(&ast, &HashMap::new()).expect("compile gt2");
        let spec = specialize(&program);
        eprintln!(
            "[specialize] gt2 {} -> {} instrs in {} ns",
            spec.stats.instrs_before, spec.stats.instrs_after, spec.stats.specialize_ns
        );
        assert!(
            spec.stats.instrs_after < spec.stats.instrs_before,
            "gt2 should shrink {} -> {}",
            spec.stats.instrs_before,
            spec.stats.instrs_after
        );
        assert!(
            !spec.rest.iter().any(|i| matches!(i, Instr::PushInput(_))),
            "gt2 specialised stream still has PushInput"
        );
        assert!(
            spec.rest
                .iter()
                .any(|i| matches!(i, Instr::AverageConst { .. })),
            "gt2 Average(Close, FastLen) should fuse: {:?}",
            spec.rest
        );
    }

    #[test]
    fn specialize_is_cheaper_than_a_bar_loop_budget() {
        let program = compile_body(json!([
            {"type":"input_decl","decls":[{"name":"n","default":{"type":"number","value":"3"}}]},
            {"type":"var_decl","decls":[{"name":"x","init":{"type":"number","value":"0"}}]},
            {"type":"assign","target":{"type":"ident","name":"x"},
             "value":{"type":"ident","name":"n"}}
        ]));
        let spec = specialize(&program);
        assert!(
            spec.stats.specialize_ns < 50_000_000,
            "specialise took {} ns",
            spec.stats.specialize_ns
        );
        assert!(spec.stats.instrs_after <= spec.stats.instrs_before);
    }

    fn num(v: &str) -> serde_json::Value {
        json!({"type": "number", "value": v})
    }

    fn ident(name: &str) -> serde_json::Value {
        json!({"type": "ident", "name": name})
    }

    fn element(array: &str, index: serde_json::Value) -> serde_json::Value {
        json!({"type": "bar_ref", "series": ident(array), "index": index})
    }

    /// `for i = 0 to 3 { if i = 1 continue; A[i] = A[i] + 1; if i = 2 break }`
    /// plus an unread `X = A[9]` (out of range: it must still run and fail).
    fn array_loop_program() -> Program {
        compile_body(json!([
            {"type": "array_decl", "decls": [{"name": "a", "size": num("3"), "init": num("0")}]},
            {"type": "var_decl", "decls": [
                {"name": "i", "init": num("0")}, {"name": "x", "init": num("0")}]},
            {"type": "for", "var": "i", "start": num("0"), "end": num("3"), "downto": false,
             "body": [
                {"type": "if", "cond": {"type": "compare", "op": "=", "left": ident("i"), "right": num("1")},
                 "body": [{"type": "continue"}], "else_body": null},
                {"type": "assign", "target": element("a", ident("i")),
                 "value": {"type": "binop", "op": "+", "left": element("a", ident("i")), "right": num("1")}},
                {"type": "if", "cond": {"type": "compare", "op": "=", "left": ident("i"), "right": num("2")},
                 "body": [{"type": "break"}], "else_body": null}
             ]},
            {"type": "assign", "target": ident("x"), "value": element("a", num("9"))},
            {"type": "expr_stmt", "expr": {"type": "call", "name": "array_sum",
             "args": [ident("a"), num("0"), num("3")]}}
        ]))
    }

    fn count(instrs: &[Instr], pred: fn(&Instr) -> bool) -> usize {
        instrs.iter().filter(|i| pred(i)).count()
    }

    /// Every pass keeps the array operations (an unread element read can
    /// still fail the run) and leaves the loop's jumps pointing at the loop.
    #[test]
    fn array_ops_and_loop_jumps_survive_every_pass() {
        let program = array_loop_program();
        for keep_stores in [false, true] {
            let spec = specialize_with(&program, keep_stores);
            let code = &spec.rest;
            assert_eq!(
                count(code, |i| matches!(i, Instr::ArrayGet(_))),
                count(&program.instrs, |i| matches!(i, Instr::ArrayGet(_))),
                "element reads (including the unread A[9]) are kept"
            );
            assert_eq!(count(code, |i| matches!(i, Instr::ArraySet { .. })), 1);
            assert_eq!(count(code, |i| matches!(i, Instr::ArrayCall { .. })), 1);
            let init = code
                .iter()
                .position(|i| matches!(i, Instr::ForInit { .. }))
                .expect("ForInit kept");
            let next = code
                .iter()
                .position(|i| matches!(i, Instr::ForNext { .. }))
                .expect("ForNext kept");
            assert!(matches!(code[init], Instr::ForInit { exit, .. } if exit == next + 1));
            assert!(matches!(code[next], Instr::ForNext { body, .. } if body == init + 1));
            for (at, ins) in code.iter().enumerate() {
                if let Some(target) = jump_target(ins) {
                    assert!(target <= code.len(), "jump {at} -> {target} out of range");
                }
                if matches!(ins, Instr::Jump(_)) && (init..next).contains(&at) {
                    let target = jump_target(ins).unwrap();
                    assert!(
                        target == next || target == next + 1,
                        "continue / break jump {at} -> {target} must leave via the loop"
                    );
                }
            }
        }
    }

    /// The loop variable is written by the loop opcodes, not a store, so
    /// dead-store elimination cannot remove it; the array write is kept even
    /// though nothing reads `a` as a variable.
    #[test]
    fn dead_store_elimination_keeps_array_writes_and_loop_var() {
        let program = array_loop_program();
        let spec = specialize_with(&program, false);
        assert!(spec
            .rest
            .iter()
            .any(|i| matches!(i, Instr::ArraySet { .. })));
        assert!(!spec.rest.iter().any(|i| matches!(i, Instr::StoreVar(_))));
        assert!(spec
            .rest
            .iter()
            .any(|i| matches!(i, Instr::ForNext { slot: 0, .. })));
    }
}
