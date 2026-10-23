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

//! Compile-time Python types of the traced names (variables and inputs).
//!
//! The kernel keeps every number as `f64`, but the Python trace prints
//! `str(value)`, so `5` (an `int`) and `5.0` (a `float`) are different cells.
//! This pass works out, before the run, which Python type each traced name
//! holds at the end of every bar, by copying the rules of the persist/trace
//! codegen:
//!
//! * a numeric variable (`Vars: X(0)`) that is not a self-referential
//!   accumulator is seeded and assigned through `f32(...)`, so it is always a
//!   `float` (`codegen._persist_seed_expr`, `codegen_stmt.gen_assign`);
//! * an accumulator (`X = X + 1`), a `true`/`false`-initialised variable (seeded
//!   as `1`/`0`) and a string variable keep the dynamic type of what is
//!   assigned, so the pass interprets the body over sets of types, joining the
//!   branches of `if` and `once`, until the bar-to-bar state is stable;
//! * expression types follow Python: `int op int` is `int` except `/`, any
//!   `float` makes a `float`, `and`/`or` return an operand, builtins return
//!   what their Python implementation returns (`int()` for IntPortion, `abs`
//!   keeps the operand type, Sign returns ints, ...).
//!
//! A name whose type depends on the path the run takes (an `int` on one branch
//! and a `float` on another), or whose Python value is not the kernel's value
//! (a `Vars: X(-1)` seed, which Python seeds as `''`), gets an error instead of
//! a type; the trace entry point refuses such a program rather than guess.

use std::cell::RefCell;
use std::collections::HashSet;

use serde_json::Value as Json;

use super::compile::is_literal_zero;
use super::ir::node_type;

/// The Python type of one traced name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TraceKind {
    Int,
    Float,
    Bool,
    Str,
}

/// A traced name's type, or why it cannot be known before the run.
pub(crate) type TraceType = Result<TraceKind, String>;

const INT: u8 = 1;
const FLOAT: u8 = 2;
const BOOL: u8 = 4;
const STR: u8 = 8;

/// A set of possible Python types, plus the first reason the value cannot be
/// reproduced at all (`bad`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
struct Ty {
    bits: u8,
    bad: Option<&'static str>,
}

impl Ty {
    const fn of(bits: u8) -> Self {
        Self { bits, bad: None }
    }

    const fn bad(reason: &'static str) -> Self {
        Self {
            bits: 0,
            bad: Some(reason),
        }
    }

    fn join(self, other: Ty) -> Ty {
        Ty {
            bits: self.bits | other.bits,
            bad: self.bad.or(other.bad),
        }
    }

    fn has(self, bit: u8) -> bool {
        self.bits & bit != 0
    }

    /// Python numeric kinds of the operand (`bool` arithmetic is `int`).
    fn numeric_bits(self) -> u8 {
        let mut out = 0;
        if self.has(INT) || self.has(BOOL) {
            out |= INT;
        }
        if self.has(FLOAT) {
            out |= FLOAT;
        }
        out
    }
}

/// One declared variable, as the Python codegen sees it.
pub(crate) struct VarDecl<'a> {
    pub(crate) name: &'a str,
    pub(crate) init: &'a Json,
    /// Declared only inside blocks: a plain Python local, bound to its init
    /// (no `f32()`) each time the declaration runs, and never traced.
    pub(crate) block: bool,
}

/// One declared input and its default expression.
pub(crate) struct InputDecl<'a> {
    pub(crate) name: &'a str,
    pub(crate) default: &'a Json,
}

/// Python types of `vars` (end of every bar) and `inputs`, in declaration
/// order. `rust_double_accum` is the kernel's own accumulator set; a variable
/// on which Python and the kernel disagree is refused (their values would
/// differ by the f32 narrowing).
pub(crate) fn infer(
    body: &[Json],
    vars: &[VarDecl<'_>],
    inputs: &[InputDecl<'_>],
    rust_double_accum: &HashSet<String>,
) -> (Vec<TraceType>, Vec<TraceType>) {
    let input_tys: Vec<Ty> = inputs.iter().map(input_ty).collect();
    // codegen._numeric_vars: only a plain number init makes a variable numeric.
    let numeric: Vec<bool> = vars
        .iter()
        .map(|v| !v.block && node_type(v.init) == Some("number"))
        .collect();
    let mut py_double_accum = HashSet::new();
    collect_double_accums(body, vars, &numeric, &mut py_double_accum);
    let seeds: Vec<Ty> = vars
        .iter()
        .map(|v| {
            if v.block {
                // Every read follows the declaration on the same bar.
                return Ty::of(0);
            }
            let accum = py_double_accum.contains(v.name);
            if accum != rust_double_accum.contains(v.name) {
                return Ty::bad("Python and the kernel narrow this variable differently");
            }
            match node_type(v.init) {
                Some("number") if !accum => Ty::of(FLOAT),
                Some("number") => literal_ty(v.init),
                Some("boolean") => Ty::of(INT),
                Some("string") => Ty::of(STR),
                _ => Ty::bad("Python seeds a variable with this initial value as ''"),
            }
        })
        .collect();
    let cx = Cx {
        vars,
        numeric: &numeric,
        accum: &py_double_accum,
        inputs,
        input_tys: &input_tys,
        loop_exits: RefCell::new(Vec::new()),
    };
    let mut start = seeds.clone();
    let mut end = start.clone();
    let mut converged = false;
    for _ in 0..64 {
        end = start.clone();
        cx.stmts(body, &start, &mut end);
        let next: Vec<Ty> = seeds.iter().zip(&end).map(|(s, e)| s.join(*e)).collect();
        if next == start {
            converged = true;
            break;
        }
        start = next;
    }
    let var_types = end
        .iter()
        .map(|ty| {
            if converged {
                resolve(*ty)
            } else {
                Err("variable types did not settle".to_owned())
            }
        })
        .collect();
    let input_types = input_tys.into_iter().map(resolve).collect();
    (var_types, input_types)
}

/// Keys of the Python bar loop's `kwargs` (`bar_loop.run_bar_loop`). The
/// codegen binds an input as `kwargs.get(name, default)`, so an input with one
/// of these names takes the engine's value instead of its default.
const BAR_LOOP_KWARGS: [&str; 62] = [
    "_first_bar",
    "_state",
    "_closed_trades",
    "avgbarseventrade",
    "avgbarslostrade",
    "avgbarswintrade",
    "avgentryprice",
    "barinterval",
    "barnumber",
    "bars_since_entry",
    "bars_since_exit",
    "bartype",
    "bigpointvalue",
    "category",
    "closed_0",
    "closed_1",
    "current_contracts",
    "current_date",
    "current_time",
    "currentbar",
    "currentsession",
    "datetime",
    "description",
    "entry_date",
    "entry_price",
    "entry_time",
    "exit_price",
    "expirationdate",
    "gross_loss",
    "gross_profit",
    "highd_0",
    "largestlostrade",
    "largestwintrade",
    "lowd_0",
    "market_position",
    "maxconseclosers",
    "maxconsecwinners",
    "maxcontractsheld",
    "maxiddrawdown",
    "maxshares",
    "minmove",
    "net_profit",
    "numeventrades",
    "numlostrades",
    "numwintrades",
    "open_position_profit",
    "opend_0",
    "openentriescount",
    "percentprofit",
    "pointvalue",
    "position_profit",
    "pricescale",
    "sess1starttime",
    "session_last_bar",
    "symbol",
    "symbolcurrencycode",
    "symbolname",
    "time_s",
    "totalbarseventrades",
    "totalbarslostrades",
    "totalbarswintrades",
    "totaltrades",
];

/// Input name the Python bar loop always binds to `''` (its sandbox for
/// `Print(File(OutPath))`); the trace entry point overrides it the same way.
pub(crate) const BLANKED_INPUT: &str = "outpath";

fn input_ty(input: &InputDecl<'_>) -> Ty {
    if input.name == BLANKED_INPUT {
        Ty::of(STR)
    } else if BAR_LOOP_KWARGS.contains(&input.name) {
        Ty::bad("Python binds an input with this name to a bar-loop value")
    } else {
        literal_ty(input.default)
    }
}

fn resolve(ty: Ty) -> TraceType {
    if let Some(reason) = ty.bad {
        return Err(reason.to_owned());
    }
    match ty.bits {
        INT => Ok(TraceKind::Int),
        FLOAT => Ok(TraceKind::Float),
        BOOL => Ok(TraceKind::Bool),
        STR => Ok(TraceKind::Str),
        _ => Err(format!(
            "its Python type depends on the run ({})",
            describe(ty.bits)
        )),
    }
}

fn describe(bits: u8) -> String {
    let names: Vec<&str> = [(INT, "int"), (FLOAT, "float"), (BOOL, "bool"), (STR, "str")]
        .iter()
        .filter(|(bit, _)| bits & bit != 0)
        .map(|(_, name)| *name)
        .collect();
    if names.is_empty() {
        "no value".to_owned()
    } else {
        names.join(" or ")
    }
}

/// Python type of a number literal: `codegen_expr.gen_expr` emits the text, so
/// a literal with a `.` (or an exponent) is a `float`, any other an `int`.
fn number_ty(node: &Json) -> Ty {
    let raw = node.get("value").and_then(Json::as_str).unwrap_or("");
    if raw.contains(['.', 'e', 'E']) {
        Ty::of(FLOAT)
    } else {
        Ty::of(INT)
    }
}

/// Type of an input default or an accumulator seed: a literal, optionally
/// under a unary sign.
fn literal_ty(node: &Json) -> Ty {
    match node_type(node) {
        Some("number") => number_ty(node),
        Some("boolean") => Ty::of(BOOL),
        Some("string") => Ty::of(STR),
        Some("unaryop") => {
            let operand = literal_ty(node.get("operand").unwrap_or(&Json::Null));
            match node.get("op").and_then(Json::as_str) {
                Some("-") | Some("+") if operand.bits & STR == 0 => Ty::of(operand.numeric_bits())
                    .join(Ty {
                        bits: 0,
                        bad: operand.bad,
                    }),
                _ => Ty::bad("unsupported literal"),
            }
        }
        _ => Ty::bad("unsupported literal"),
    }
}

/// `codegen._collect_double_accums`: a numeric variable assigned from an
/// expression that references it (`codegen._expr_refs`) is kept in double.
fn collect_double_accums(
    stmts: &[Json],
    vars: &[VarDecl<'_>],
    numeric: &[bool],
    out: &mut HashSet<String>,
) {
    for stmt in stmts {
        if node_type(stmt) == Some("assign") {
            let target = stmt.get("target").unwrap_or(&Json::Null);
            if node_type(target) == Some("ident") {
                if let Some(name) = target.get("name").and_then(Json::as_str) {
                    let is_numeric = vars.iter().zip(numeric).any(|(v, n)| *n && v.name == name);
                    if is_numeric {
                        let mut refs = HashSet::new();
                        expr_refs(stmt.get("value").unwrap_or(&Json::Null), &mut refs);
                        if refs.contains(name) {
                            out.insert(name.to_owned());
                        }
                    }
                }
            }
        }
        for key in ["body", "else_body"] {
            if let Some(Json::Array(body)) = stmt.get(key) {
                collect_double_accums(body, vars, numeric, out);
            }
        }
    }
}

/// `codegen._expr_refs`: identifier names an expression reads.
fn expr_refs(node: &Json, names: &mut HashSet<String>) {
    let Json::Object(map) = node else {
        return;
    };
    match map.get("type").and_then(Json::as_str) {
        Some("ident") => {
            if let Some(name) = map.get("name").and_then(Json::as_str) {
                names.insert(name.to_owned());
            }
        }
        Some("bar_ref") => {
            if let Some(series) = map.get("series") {
                if node_type(series) == Some("ident") {
                    if let Some(name) = series.get("name").and_then(Json::as_str) {
                        names.insert(name.to_owned());
                    }
                }
            }
            expr_refs(map.get("index").unwrap_or(&Json::Null), names);
        }
        _ => {}
    }
    for key in [
        "left", "right", "operand", "cond", "msg", "value", "init", "start", "end", "index",
    ] {
        if let Some(child) = map.get(key) {
            expr_refs(child, names);
        }
    }
    for key in ["args", "decls"] {
        if let Some(Json::Array(children)) = map.get(key) {
            for child in children {
                expr_refs(child, names);
            }
        }
    }
}

struct Cx<'a, 'b> {
    vars: &'a [VarDecl<'b>],
    numeric: &'a [bool],
    accum: &'a HashSet<String>,
    inputs: &'a [InputDecl<'b>],
    input_tys: &'a [Ty],
    /// Variable types at each `break` / `continue` of the enclosing loops.
    loop_exits: RefCell<Vec<LoopExits>>,
}

#[derive(Default)]
struct LoopExits {
    breaks: Option<Vec<Ty>>,
    continues: Option<Vec<Ty>>,
}

fn join_all(into: &mut [Ty], other: &[Ty]) {
    for (a, b) in into.iter_mut().zip(other) {
        *a = a.join(*b);
    }
}

fn join_into(slot: &mut Option<Vec<Ty>>, env: &[Ty]) {
    match slot {
        Some(acc) => join_all(acc, env),
        None => *slot = Some(env.to_vec()),
    }
}

/// A loop bound known before the run: an integer literal, an input (a
/// trace run uses the defaults), or a sum / difference of those.
fn int_bound(node: &Json, inputs: &[InputDecl<'_>]) -> Option<i64> {
    match node_type(node) {
        Some("number") => node.get("value")?.as_str()?.parse().ok(),
        Some("unaryop") if node.get("op").and_then(Json::as_str) == Some("-") => {
            int_bound(node.get("operand")?, inputs)?.checked_neg()
        }
        Some("ident") => {
            let name = node.get("name").and_then(Json::as_str)?;
            let input = inputs.iter().find(|i| i.name == name)?;
            if BAR_LOOP_KWARGS.contains(&name) {
                return None;
            }
            int_bound(input.default, inputs)
        }
        Some("binop") => {
            let left = int_bound(node.get("left")?, inputs)?;
            let right = int_bound(node.get("right")?, inputs)?;
            match node.get("op").and_then(Json::as_str)? {
                "+" => left.checked_add(right),
                "-" => left.checked_sub(right),
                _ => None,
            }
        }
        _ => None,
    }
}

impl Cx<'_, '_> {
    fn var_slot(&self, name: &str) -> Option<usize> {
        self.vars.iter().position(|v| v.name == name)
    }

    /// Walk `stmts`, updating `env` (the current type of every variable).
    /// `start` is the type at the start of the bar, used for lookbacks.
    fn stmts(&self, stmts: &[Json], start: &[Ty], env: &mut Vec<Ty>) {
        for stmt in stmts {
            match node_type(stmt) {
                Some("assign") => {
                    let target = stmt.get("target").unwrap_or(&Json::Null);
                    let Some(slot) = target
                        .get("name")
                        .and_then(Json::as_str)
                        .and_then(|n| self.var_slot(n))
                    else {
                        continue;
                    };
                    let value = self.expr(stmt.get("value").unwrap_or(&Json::Null), start, env);
                    let name = self.vars[slot].name;
                    env[slot] = if self.numeric[slot] && !self.accum.contains(name) {
                        // `f32(...)`: always a float; a string makes Python raise.
                        if value.has(STR) {
                            Ty::bad("f32() of a string")
                        } else {
                            Ty {
                                bits: FLOAT,
                                bad: value.bad,
                            }
                        }
                    } else {
                        value
                    };
                }
                Some("if") => {
                    let mut other = env.clone();
                    if let Some(Json::Array(body)) = stmt.get("body") {
                        self.stmts(body, start, env);
                    }
                    if let Some(Json::Array(body)) = stmt.get("else_body") {
                        self.stmts(body, start, &mut other);
                    }
                    for (a, b) in env.iter_mut().zip(other) {
                        *a = a.join(b);
                    }
                }
                Some("var_decl") => {
                    // A top-level (or redeclared top-level) name is not
                    // rebound; a block-declared one is `name = <init>`.
                    for decl in stmt
                        .get("decls")
                        .and_then(Json::as_array)
                        .into_iter()
                        .flatten()
                    {
                        let slot = decl
                            .get("name")
                            .and_then(Json::as_str)
                            .and_then(|n| self.var_slot(n))
                            .filter(|slot| self.vars[*slot].block);
                        if let Some(slot) = slot {
                            env[slot] =
                                self.expr(decl.get("init").unwrap_or(&Json::Null), start, env);
                        }
                    }
                }
                Some("for") => self.for_loop(stmt, start, env),
                Some(kind @ ("break" | "continue")) => {
                    if let Some(exits) = self.loop_exits.borrow_mut().last_mut() {
                        let slot = if kind == "break" {
                            &mut exits.breaks
                        } else {
                            &mut exits.continues
                        };
                        join_into(slot, env);
                    }
                }
                Some("once") => {
                    let skipped = env.clone();
                    if let Some(Json::Array(body)) = stmt.get("body") {
                        self.stmts(body, start, env);
                    }
                    for (a, b) in env.iter_mut().zip(skipped) {
                        *a = a.join(b);
                    }
                }
                _ => {}
            }
        }
    }

    /// `for var in range(int(start), int(end) + 1)`: `var` is an `int` at
    /// the top of every iteration; the loop leaves the state of the last
    /// body pass (or a `continue` / `break` in it), or the state before the
    /// loop when the range can be empty.
    fn for_loop(&self, stmt: &Json, start: &[Ty], env: &mut Vec<Ty>) {
        let first = stmt.get("start").unwrap_or(&Json::Null);
        let last = stmt.get("end").unwrap_or(&Json::Null);
        let bad = self
            .expr(first, start, env)
            .bad
            .or(self.expr(last, start, env).bad);
        let slot = stmt
            .get("var")
            .and_then(Json::as_str)
            .and_then(|n| self.var_slot(n));
        let body: &[Json] = stmt
            .get("body")
            .and_then(Json::as_array)
            .map(|a| a.as_slice())
            .unwrap_or(&[]);
        let downto = stmt.get("downto").and_then(Json::as_bool).unwrap_or(false);
        let always_runs = match (int_bound(first, self.inputs), int_bound(last, self.inputs)) {
            (Some(a), Some(b)) => (downto && a >= b) || (!downto && a <= b),
            _ => false,
        };
        let set_var = |env: &mut Vec<Ty>| {
            if let Some(slot) = slot {
                env[slot] = Ty { bits: INT, bad };
            }
        };
        let before = env.clone();
        let mut head = before.clone();
        set_var(&mut head);
        // Types only grow and each name has four, so this settles.
        let mut exit;
        loop {
            let mut tail = head.clone();
            self.loop_exits.borrow_mut().push(LoopExits::default());
            self.stmts(body, start, &mut tail);
            let exits = self.loop_exits.borrow_mut().pop().unwrap_or_default();
            if let Some(continues) = &exits.continues {
                join_all(&mut tail, continues);
            }
            exit = tail.clone();
            if let Some(breaks) = &exits.breaks {
                join_all(&mut exit, breaks);
            }
            let mut next = head.clone();
            join_all(&mut next, &tail);
            set_var(&mut next);
            if next == head {
                break;
            }
            head = next;
        }
        if !always_runs {
            join_all(&mut exit, &before);
        }
        *env = exit;
    }

    fn expr(&self, node: &Json, start: &[Ty], env: &[Ty]) -> Ty {
        match node_type(node) {
            Some("number") => number_ty(node),
            Some("string") => Ty::of(STR),
            Some("boolean") => Ty::of(BOOL),
            Some("ident") => self.ident(node, env),
            Some("binop") => {
                let left = self.expr(node.get("left").unwrap_or(&Json::Null), start, env);
                let right = self.expr(node.get("right").unwrap_or(&Json::Null), start, env);
                binop_ty(
                    node.get("op").and_then(Json::as_str).unwrap_or(""),
                    left,
                    right,
                )
            }
            Some("compare") => {
                let left = self.expr(node.get("left").unwrap_or(&Json::Null), start, env);
                let right = self.expr(node.get("right").unwrap_or(&Json::Null), start, env);
                Ty {
                    bits: BOOL,
                    bad: left.bad.or(right.bad),
                }
            }
            Some("unaryop") => {
                let operand = self.expr(node.get("operand").unwrap_or(&Json::Null), start, env);
                match node.get("op").and_then(Json::as_str) {
                    Some("not") => Ty {
                        bits: BOOL,
                        bad: operand.bad,
                    },
                    _ if operand.has(STR) => Ty::bad("unary sign of a string"),
                    _ => Ty {
                        bits: operand.numeric_bits(),
                        bad: operand.bad,
                    },
                }
            }
            Some("call") => self.call(node, start, env),
            Some("position_kw") => {
                position_kw_ty(node.get("kwarg").and_then(Json::as_str).unwrap_or(""))
            }
            Some("mc_kw") => Ty::of(INT),
            Some("bar_ref") => self.bar_ref(node, start, env),
            Some("data_of") => match node.get("series").and_then(Json::as_str) {
                Some("date") | Some("time") => Ty::bad("Data2 date/time type"),
                _ => Ty::of(FLOAT),
            },
            _ => Ty::bad("expression type unknown to the trace typer"),
        }
    }

    fn ident(&self, node: &Json, env: &[Ty]) -> Ty {
        let name = node.get("name").and_then(Json::as_str).unwrap_or("");
        match name {
            "_market_position" => return Ty::of(INT),
            "open" | "high" | "low" | "close" | "volume" => return Ty::of(FLOAT),
            "date" | "time" => return Ty::of(INT),
            _ => {}
        }
        if let Some(slot) = self.var_slot(name) {
            return env[slot];
        }
        if let Some(slot) = self.inputs.iter().position(|i| i.name == name) {
            return self.input_tys[slot];
        }
        match name {
            // pl_tickid() / pl_execoffset() return the int 0.
            "tickid" | "execoffset" => Ty::of(INT),
            n if super::compile::bare_session_time(n).is_some() => Ty::of(INT),
            "lastbaronchart_s" => Ty::of(BOOL),
            // pl_currenttime_s() / pl_currentdate_s(): ints; ComputerDateTime a float.
            "currenttime_s" | "currentdate_s" => Ty::of(INT),
            "computerdatetime" => Ty::of(FLOAT),
            "newline" => Ty::of(STR),
            _ => Ty::bad("identifier unknown to the trace typer"),
        }
    }

    fn bar_ref(&self, node: &Json, start: &[Ty], env: &[Ty]) -> Ty {
        let series = node.get("series").unwrap_or(&Json::Null);
        let index = self.expr(node.get("index").unwrap_or(&Json::Null), start, env);
        let ty = match node_type(series) {
            Some("ident") => {
                let name = series.get("name").and_then(Json::as_str).unwrap_or("");
                match name {
                    // A raw (un-narrowed) element of the float price list.
                    "open" | "high" | "low" | "close" | "volume" => Ty::of(FLOAT),
                    "date" | "time" => Ty::of(INT),
                    _ => match self.var_slot(name) {
                        // Any earlier bar's value, or the seed when there is
                        // not enough history yet.
                        Some(slot) => start[slot].join(env[slot]),
                        // Input[n] is the input itself (the index is dropped).
                        None => match self.inputs.iter().position(|i| i.name == name) {
                            Some(slot) => self.input_tys[slot],
                            None => Ty::bad("lookback of an unknown series"),
                        },
                    },
                }
            }
            Some("position_kw") => {
                let kw = series.get("kwarg").and_then(Json::as_str).unwrap_or("");
                let literal_zero = is_literal_zero(node.get("index"));
                match kw {
                    // pl_pos_ago(kwargs, field, 0) is the bare keyword.
                    "entry_price" | "exit_price" if literal_zero => position_kw_ty(kw),
                    // n >= 1 reads the ledger's f32 price, or the int 0 past
                    // the oldest closed position.
                    "entry_price" | "exit_price" => position_kw_ty(kw).join(Ty::of(INT)),
                    // Ints in the bare keyword and the ledger alike.
                    "entry_date" | "entry_time" | "exit_date" | "exit_time" | "maxshares" => {
                        Ty::of(INT)
                    }
                    _ => Ty::bad("positions-ago keyword type"),
                }
            }
            _ => Ty::bad("lookback of an unknown series"),
        };
        Ty {
            bits: ty.bits,
            bad: ty.bad.or(index.bad),
        }
    }

    fn call(&self, node: &Json, start: &[Ty], env: &[Ty]) -> Ty {
        let name = node.get("name").and_then(Json::as_str).unwrap_or("");
        let args: Vec<&Json> = node
            .get("args")
            .and_then(Json::as_array)
            .map(|a| a.iter().collect())
            .unwrap_or_default();
        match name {
            // pl_average: `0.0` or `f32(...)`.
            "average" | "averagefc" | "averagetfc" => return Ty::of(FLOAT),
            // pl_crosses_*: a comparison or `False`.
            "crossesabove" | "crossesbelow" => return Ty::of(BOOL),
            _ => {}
        }
        let tys: Vec<Ty> = args.iter().map(|a| self.expr(a, start, env)).collect();
        let bad = tys.iter().find_map(|t| t.bad);
        let with_bad = |ty: Ty| Ty {
            bits: ty.bits,
            bad: ty.bad.or(bad),
        };
        match name {
            // f"{x:.{n}f}": n must be an int, x a number.
            "numtostr" => {
                let ok = tys.len() == 2 && !tys[0].has(STR) && tys[1].bits == INT;
                if ok {
                    with_bad(Ty::of(STR))
                } else {
                    Ty::bad("NumToStr argument types")
                }
            }
            // kwargs.get('entry_name' / 'exit_name', '')
            "entryname" | "exitname" if !tys.is_empty() => with_bad(Ty::of(STR)),
            // abs() keeps int/float (a bool becomes an int).
            "absvalue" | "abs" => match tys.first() {
                Some(t) if !t.has(STR) => with_bad(Ty::of(t.numeric_bits())),
                _ => Ty::bad("AbsValue argument type"),
            },
            // int(x)
            "intportion" => with_bad(Ty::of(INT)),
            // math.floor(x) and pl_datetojulian(d) return ints.
            "floor" => match tys.as_slice() {
                [t] if !t.has(STR) => with_bad(Ty::of(INT)),
                _ => Ty::bad("Floor argument type"),
            },
            "eldatetodatetime" => match tys.as_slice() {
                [t] if !t.has(STR) => with_bad(Ty::of(INT)),
                // `(d * 10000 + t)`
                [d, t] => binop_ty("+", binop_ty("*", *d, Ty::of(INT)), *t),
                _ => Ty::bad("ELDateToDateTime argument count"),
            },
            "currenttime_s" | "currentdate_s" => with_bad(Ty::of(INT)),
            "computerdatetime" => with_bad(Ty::of(FLOAT)),
            // `1 if x > 0 else (-1 if x < 0 else 0)`
            "sign" => with_bad(Ty::of(INT)),
            // `x ** 2`
            "square" => match tys.first() {
                Some(t) if !t.has(STR) => with_bad(Ty::of(t.numeric_bits())),
                _ => Ty::bad("Square argument type"),
            },
            // el_round0: math.floor's int; el_round: that int / 10 ** n.
            "round" => match tys.as_slice() {
                [x] if !x.has(STR) => with_bad(Ty::of(INT)),
                [x, n] if !x.has(STR) && !n.has(STR) => with_bad(Ty::of(FLOAT)),
                _ => Ty::bad("Round argument types"),
            },
            // max()/min() return one of the arguments.
            "maxlist" | "minlist" => {
                if tys.iter().any(|t| t.has(STR)) {
                    return Ty::bad("MaxList/MinList of a string");
                }
                tys.iter().fold(Ty::default(), |acc, t| acc.join(*t))
            }
            // Colour helpers build ints.
            "rgb" | "legacycolortorgb" | "gradientcolor" => with_bad(Ty::of(INT)),
            // Session-template words return an int literal (_rt_session).
            n if super::compile::session_time(n).is_some() => with_bad(Ty::of(INT)),
            "currentsession" => with_bad(Ty::of(INT)),
            // __builtin_len(s)
            "strlen" => match tys.as_slice() {
                [t] if t.bits == STR => with_bad(Ty::of(INT)),
                _ => Ty::bad("StrLen argument type"),
            },
            // pl_formatdate returns a str ('' with fewer than two args).
            "formatdate" => with_bad(Ty::of(STR)),
            // pl_file returns its path; pl_text_setcolor and the file stubs 0.
            "file" => match tys.as_slice() {
                [t] if t.bits == STR => with_bad(Ty::of(STR)),
                _ => Ty::bad("File argument type"),
            },
            "text_setcolor" | "fileappend" | "filedelete" => with_bad(Ty::of(INT)),
            // Drawing ids come from a process-wide counter in Python.
            _ => Ty::bad("call result type unknown to the trace typer"),
        }
    }
}

/// Python arithmetic result types (`codegen_expr` emits the plain operator).
fn binop_ty(op: &str, left: Ty, right: Ty) -> Ty {
    let bad = left.bad.or(right.bad);
    if op == "and" || op == "or" {
        // Python returns one of the operands.
        return left.join(right);
    }
    if left.has(STR) || right.has(STR) {
        let pure_strings = left.bits == STR && right.bits == STR;
        if op == "+" && pure_strings {
            return Ty { bits: STR, bad };
        }
        return Ty::bad("string mixed with a number");
    }
    let (l, r) = (left.numeric_bits(), right.numeric_bits());
    let mut bits = 0;
    if op == "/" {
        if l != 0 && r != 0 {
            bits = FLOAT;
        }
    } else {
        if l & INT != 0 && r & INT != 0 {
            bits |= INT;
        }
        if (l & FLOAT != 0 && r != 0) || (r & FLOAT != 0 && l != 0) {
            bits |= FLOAT;
        }
    }
    Ty { bits, bad }
}

/// Python types of the engine values a position keyword reads
/// (`FillEngine` attributes and the bar loop's kwargs).
fn position_kw_ty(kw: &str) -> Ty {
    match kw {
        "barnumber" | "currentbar" | "market_position" | "current_contracts"
        | "bars_since_entry" | "bars_since_exit" | "totaltrades" | "maxcontractsheld"
        | "current_date" | "current_time" | "currentsession" | "minmove" | "pricescale"
        | "entry_date" | "entry_time" | "exit_date" | "exit_time" | "maxshares" => Ty::of(INT),
        "entry_price"
        | "avgentryprice"
        | "exit_price"
        | "open_position_profit"
        | "position_profit"
        | "net_profit"
        | "gross_profit"
        | "gross_loss"
        | "maxiddrawdown" => Ty::of(FLOAT),
        "alert_enabled" | "check_alert" | "lastbaronchartex" => Ty::of(BOOL),
        _ => Ty::bad("position keyword type"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn types(src_body: Json, vars: &[(&str, Json)], accum: &[&str]) -> Vec<TraceType> {
        let body = src_body.as_array().unwrap().clone();
        let decls: Vec<VarDecl<'_>> = vars
            .iter()
            .map(|(name, init)| VarDecl {
                name,
                init,
                block: false,
            })
            .collect();
        let accum: HashSet<String> = accum.iter().map(|s| (*s).to_owned()).collect();
        infer(&body, &decls, &[], &accum).0
    }

    fn num(v: &str) -> Json {
        json!({"type": "number", "value": v})
    }

    fn ident(n: &str) -> Json {
        json!({"type": "ident", "name": n})
    }

    fn assign(n: &str, value: Json) -> Json {
        json!({"type": "assign", "target": ident(n), "value": value})
    }

    fn add(l: Json, r: Json) -> Json {
        json!({"type": "binop", "op": "+", "left": l, "right": r})
    }

    #[test]
    fn int_accumulator_stays_int_and_numeric_var_is_float() {
        let body = json!([
            assign("c", add(ident("c"), num("1"))),
            assign("h", ident("c"))
        ]);
        let t = types(body, &[("c", num("0")), ("h", num("0"))], &["c"]);
        assert_eq!(t, vec![Ok(TraceKind::Int), Ok(TraceKind::Float)]);
    }

    #[test]
    fn float_entering_an_int_accumulator_on_one_branch_is_refused() {
        let body = json!([{
            "type": "if",
            "cond": {"type": "boolean", "value": true},
            "body": [assign("c", add(ident("c"), num("1.5")))],
            "else_body": [assign("c", add(ident("c"), num("1")))]
        }]);
        let t = types(body, &[("c", num("0"))], &["c"]);
        assert!(t[0].as_ref().unwrap_err().contains("int or float"));
    }

    #[test]
    fn float_seed_makes_an_accumulator_float() {
        let body = json!([assign("c", add(ident("c"), num("1")))]);
        let t = types(body, &[("c", num("0.5"))], &["c"]);
        assert_eq!(t, vec![Ok(TraceKind::Float)]);
    }

    #[test]
    fn bool_var_is_int_until_assigned_unconditionally() {
        let cmp = json!({"type": "compare", "op": ">", "left": ident("close"), "right": num("1")});
        let always = types(
            json!([assign("b", cmp.clone())]),
            &[("b", json!({"type": "boolean", "value": false}))],
            &[],
        );
        assert_eq!(always, vec![Ok(TraceKind::Bool)]);
        let sometimes = types(
            json!([{"type": "if", "cond": cmp.clone(), "body": [assign("b", cmp)]}]),
            &[("b", json!({"type": "boolean", "value": false}))],
            &[],
        );
        assert!(sometimes[0].is_err());
    }

    #[test]
    fn unary_minus_seed_is_refused_and_division_is_float() {
        let neg = json!({"type": "unaryop", "op": "-", "operand": num("1")});
        let div = json!({"type": "binop", "op": "/", "left": ident("c"), "right": num("2")});
        let t = types(
            json!([assign("d", div)]),
            &[("u", neg), ("c", num("0")), ("d", num("0"))],
            &[],
        );
        assert!(t[0].is_err());
        assert_eq!(t[2], Ok(TraceKind::Float));
    }

    #[test]
    fn mismatched_accumulator_detection_is_refused() {
        let t = types(json!([]), &[("c", num("0"))], &["c"]);
        assert!(t[0].is_err());
    }

    #[test]
    fn binop_rules_follow_python() {
        let (i, f, b, s) = (Ty::of(INT), Ty::of(FLOAT), Ty::of(BOOL), Ty::of(STR));
        assert_eq!(binop_ty("+", i, i).bits, INT);
        assert_eq!(binop_ty("+", b, i).bits, INT);
        assert_eq!(binop_ty("*", i, f).bits, FLOAT);
        assert_eq!(binop_ty("/", i, i).bits, FLOAT);
        assert_eq!(binop_ty("%", i, i).bits, INT);
        assert_eq!(binop_ty("+", s, s).bits, STR);
        assert!(binop_ty("+", s, i).bad.is_some());
        assert_eq!(binop_ty("or", i, f).bits, INT | FLOAT);
    }
}
