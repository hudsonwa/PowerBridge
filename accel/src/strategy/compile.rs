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

//! Lower the v2 program IR into a flat `Vec<Instr>` once, before the bar loop.
//!
//! The interpreter previously re-walked a `serde_json::Value` tree per bar:
//! `exec_stmt` dispatched on a node-TYPE STRING and fetched each operand with a
//! per-statement `stmt.get("key")` hash lookup. This module lowers the AST a
//! single time into typed opcodes with resolved operand indices, so the per-bar
//! loop walks a contiguous array and dispatches on an integer discriminant.
//!
//! Anything that cannot be compiled is rejected here (fail-loud) — never
//! approximated.

use std::collections::{HashMap, HashSet};

use serde_json::Value as Json;

use crate::types::{Action, KernelError, OrderType, Timing};

use super::ir::node_type;
use super::value::Value;

/// A primary data-series column (also used by `bar_ref` / `data_of`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Series {
    Open,
    High,
    Low,
    Close,
    Volume,
    Date,
    Time,
}

impl Series {
    fn from_name(name: &str) -> Option<Self> {
        Some(match name {
            "open" => Series::Open,
            "high" => Series::High,
            "low" => Series::Low,
            "close" => Series::Close,
            "volume" => Series::Volume,
            "date" => Series::Date,
            "time" => Series::Time,
            _ => return None,
        })
    }

    /// OHLCV columns carry an f32 narrowing at scalar access; date/time do not.
    pub(crate) fn is_ohlcv(self) -> bool {
        !matches!(self, Series::Date | Series::Time)
    }

    #[allow(dead_code)]
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Series::Open => "open",
            Series::High => "high",
            Series::Low => "low",
            Series::Close => "close",
            Series::Volume => "volume",
            Series::Date => "date",
            Series::Time => "time",
        }
    }
}

/// A series argument resolved zero-copy for `average` / `crosses`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SeriesSrc {
    /// A primary data-series column (OHLCV -> borrowed slice, date/time -> owned).
    Column(Series),
    /// A numeric variable's `num_history` (borrowed history + current).
    NumericVar(usize),
    /// A non-numeric variable's `history` (materialised to float).
    ValueVar(usize),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Binop {
    Add,
    Sub,
    Mul,
    Div,
    Mod,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Cmp {
    Eq,
    Ne,
    Lt,
    Gt,
    Le,
    Ge,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Unary {
    Neg,
    Not,
    Pos,
}

/// A position keyword (`position_kw` node), resolved to a scalar value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PositionKw {
    Barnumber,
    MarketPosition,
    CurrentContracts,
    EntryPrice,
    AvgEntryPrice,
    BarsSinceEntry,
    ExitPrice,
    BarsSinceExit,
    OpenPositionProfit,
    PositionProfit,
    NetProfit,
    GrossProfit,
    GrossLoss,
    TotalTrades,
    MaxContractsHeld,
    MaxIdDrawdown,
    CurrentDate,
    CurrentTime,
    AlertEnabled,
    CheckAlert,
    LastBarOnChartEx,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum McKw {
    TimeS,
    BarTypeEx,
}

/// A supported simple call (non-series, non-`average`/`crosses`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CallOp {
    NumToStr,
    File,
    AbsValue,
    IntPortion,
    Sign,
    Square,
    MaxList,
    MinList,
    TlNew,
    TlSetColor,
    TlSetStyle,
    ArwNew,
    TextNew,
    Rgb,
    LegacyColorToRgb,
    GradientColor,
}

/// A supported `Set*` money-management risk verb.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RiskOp {
    StopLoss,
    ProfitTarget,
    BreakEven,
    PercentTrailing,
    ExitOnClose,
    StopLossPt,
    ProfitTargetPt,
    BreakEvenPt,
    PercentTrailingPt,
    DollarTrailing,
    TrailingStopPt,
    Noop,
}

/// A single flattened instruction. Operands are resolved indices / enums baked
/// in at compile time; the runtime only dispatches on the discriminant.
///
/// The `*C` / `*Const` / `Series*` / `Var*` / `EmitOrderConst` variants are
/// produced by [`super::specialize::specialize`]: they bake frozen Inputs and
/// other proven constants into the opcode so the bar loop does not `PushInput`
/// or re-dispatch a load/binop/compare/store sequence. Arithmetic order is
/// unchanged — fusion never reassociates.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Instr {
    PushNum(f64),
    PushBool(bool),
    PushStr(String),
    PushInput(usize),
    PushVar(usize),
    PushVarSeries(usize),
    PushSeriesParam(Series),
    PushSeriesParamSeries(Series),
    PushMarketPosition,
    PushPositionContracts,
    PushPositionKw(PositionKw),
    PushMcKw(McKw),

    Jump(usize),
    JumpIfFalse(usize),
    JumpIfTruthyPeek(usize),
    JumpIfNotTruthyPeek(usize),
    JumpIfNotFirstBar(usize),
    SetFirstBarFalse,
    Drop,

    Binop(Binop),
    Compare(Cmp),
    Unary(Unary),
    Call(CallOp, u8),

    Average(SeriesSrc),
    Crosses {
        above: bool,
        a: SeriesSrc,
        b: SeriesSrc,
    },

    BarRefSeries(Series),
    BarRefVar(usize),
    BarRefPositionKw(PositionKw),
    DataOf {
        series: Series,
        data_num: u8,
    },

    LoadVar(usize),
    StoreVar(usize),
    EmitOrder {
        action: Action,
        order_type: OrderType,
        timing: Timing,
        has_price: bool,
    },
    RiskSet(RiskOp),

    /// Tombstone used only inside the specialiser; stripped before the bar loop.
    Nop,

    /// Numeric binop with baked RHS (TOS is the left operand). Infallible.
    BinopC {
        op: Binop,
        rhs: f64,
    },
    /// Numeric binop with baked LHS (TOS is the right operand). Infallible.
    #[allow(dead_code)]
    BinopCLhs {
        op: Binop,
        lhs: f64,
    },
    /// Compare TOS (left) with a baked numeric RHS. Pushes Bool.
    CompareC {
        cmp: Cmp,
        rhs: f64,
    },
    /// Compare a baked numeric LHS with TOS (right). Pushes Bool.
    #[allow(dead_code)]
    CompareCLhs {
        cmp: Cmp,
        lhs: f64,
    },
    /// `Average` with a baked length (Inputs fold to a constant).
    AverageConst {
        src: SeriesSrc,
        length: f64,
    },
    /// Bar-ref with a baked index.
    BarRefSeriesConst {
        series: Series,
        index: i64,
    },
    BarRefVarConst {
        slot: usize,
        index: i64,
    },
    /// `series cmp rhs` with both the column and the RHS baked.
    SeriesCompareC {
        series: Series,
        cmp: Cmp,
        rhs: f64,
    },
    /// `series op rhs` (e.g. `Close * 2`).
    SeriesBinopC {
        series: Series,
        op: Binop,
        rhs: f64,
    },
    /// `lhs op series` (e.g. `6 * Open`).
    SeriesBinopCLhs {
        series: Series,
        op: Binop,
        lhs: f64,
    },
    /// `var[slot] op rhs`.
    VarBinopC {
        slot: usize,
        op: Binop,
        rhs: f64,
    },
    /// `lhs op var[slot]`.
    VarBinopCLhs {
        slot: usize,
        op: Binop,
        lhs: f64,
    },
    /// `var[slot] cmp rhs`.
    VarCompareC {
        slot: usize,
        cmp: Cmp,
        rhs: f64,
    },
    /// `lhs cmp var[slot]`.
    VarCompareCLhs {
        slot: usize,
        cmp: Cmp,
        lhs: f64,
    },
    /// Market/close order with baked quantity and signal (no stack traffic).
    EmitOrderConst {
        action: Action,
        order_type: OrderType,
        timing: Timing,
        quantity: f64,
        signal: String,
    },
    /// `Sell All` / `Buy All`: quantity from `position.contracts`, baked signal.
    EmitOrderAll {
        action: Action,
        order_type: OrderType,
        timing: Timing,
        signal: String,
    },
}

/// The compiled program: a flat instruction stream plus the metadata the
/// runtime needs to move values between bars (history, f32 narrowing, inputs).
///
/// `Clone` exists only for the sweep/batch path: the instruction stream and
/// per-variable metadata are identical across variants (an input override
/// changes only `inputs`, see [`resolve_inputs`] / [`resolve_input_value`]);
/// a batch runner clones the compiled program once per variant and swaps in the
/// variant's resolved inputs.
#[derive(Debug, Clone)]
pub(crate) struct Program {
    pub(crate) instrs: Vec<Instr>,

    // Per-variable metadata, indexed by var slot.
    pub(crate) var_names: Vec<String>,
    pub(crate) var_numeric: Vec<bool>,
    pub(crate) var_double_accum: Vec<bool>,
    pub(crate) var_inits: Vec<Value>,
    pub(crate) stateful: Vec<usize>,
    // Per-slot flags: which slots must keep a growing bar history because their
    // value is observable as a series (BarRefVar read, average/crosses arg).
    // A variable that is only ever read as a scalar keeps no history at all —
    // the interpreter retains only its current value across bars.
    pub(crate) var_needs_value_history: Vec<bool>,
    pub(crate) var_needs_numeric_history: Vec<bool>,

    // Resolved inputs, indexed by input slot (override > projected > default).
    pub(crate) inputs: Vec<Value>,
}

pub(crate) fn compile(
    ast: &Json,
    input_overrides: &HashMap<String, Value>,
) -> Result<Program, KernelError> {
    let body = body_of(ast)?.to_vec();
    let mut compiler = Compiler::new();
    compiler.collect_decls(&body, input_overrides)?;
    compiler.collect_double_accums(&body);
    for stmt in &body {
        compiler.compile_stmt(stmt)?;
    }
    Ok(compiler.finish())
}

struct Compiler {
    instrs: Vec<Instr>,
    var_slots: HashMap<String, usize>,
    input_slots: HashMap<String, usize>,
    numeric_vars: HashSet<String>,
    double_accum: HashSet<String>,
    stateful: Vec<String>,
    var_names: Vec<String>,
    var_inits: Vec<Value>,
    inputs: Vec<Value>,
    needs_value_history: HashSet<String>,
    needs_numeric_history: HashSet<String>,
}

impl Compiler {
    fn new() -> Self {
        Self {
            instrs: Vec::new(),
            var_slots: HashMap::new(),
            input_slots: HashMap::new(),
            numeric_vars: HashSet::new(),
            double_accum: HashSet::new(),
            stateful: Vec::new(),
            var_names: Vec::new(),
            var_inits: Vec::new(),
            inputs: Vec::new(),
            needs_value_history: HashSet::new(),
            needs_numeric_history: HashSet::new(),
        }
    }

    fn finish(self) -> Program {
        let mut var_numeric = vec![false; self.var_names.len()];
        let mut var_double_accum = vec![false; self.var_names.len()];
        let var_needs_value_history: Vec<bool> = self
            .var_names
            .iter()
            .map(|name| self.needs_value_history.contains(name))
            .collect();
        let var_needs_numeric_history: Vec<bool> = self
            .var_names
            .iter()
            .map(|name| self.needs_numeric_history.contains(name))
            .collect();
        for (i, name) in self.var_names.iter().enumerate() {
            var_numeric[i] = self.numeric_vars.contains(name);
            var_double_accum[i] = self.double_accum.contains(name);
        }
        let stateful = self
            .stateful
            .iter()
            .map(|name| self.var_slots[name])
            .collect();
        Program {
            instrs: self.instrs,
            var_names: self.var_names,
            var_numeric,
            var_double_accum,
            var_inits: self.var_inits,
            stateful,
            var_needs_value_history,
            var_needs_numeric_history,
            inputs: self.inputs,
        }
    }

    fn collect_decls(
        &mut self,
        body: &[Json],
        overrides: &HashMap<String, Value>,
    ) -> Result<(), KernelError> {
        for stmt in body {
            match node_type(stmt) {
                Some("input_decl") => {
                    for decl in decls_of(stmt)? {
                        let name = decl_name(decl)?.to_string();
                        let default = decl.get("default").ok_or_else(|| {
                            KernelError::input(format!("input {name} has no default"))
                        })?;
                        let value = resolve_input_value(&name, default, overrides)?;
                        if matches!(default.get("type").and_then(Json::as_str), Some("number")) {
                            self.numeric_vars.insert(name.clone());
                        }
                        let slot = self.inputs.len();
                        self.input_slots.insert(name, slot);
                        self.inputs.push(value);
                    }
                }
                Some("var_decl") => {
                    for decl in decls_of(stmt)? {
                        let name = decl_name(decl)?.to_string();
                        if decl.get("data_ref").is_some_and(|v| !v.is_null()) {
                            return Err(KernelError::unsupported(format!(
                                "Data2-tied variable {name}"
                            )));
                        }
                        let init = decl
                            .get("init")
                            .ok_or_else(|| KernelError::input(format!("var {name} has no init")))?;
                        let init_ty = node_type(init);
                        match init_ty {
                            Some("number") => {
                                self.numeric_vars.insert(name.clone());
                            }
                            Some("boolean") | Some("string") | Some("unaryop") => {}
                            other => {
                                return Err(KernelError::unsupported(format!(
                                    "var {name} init type {other:?}"
                                )));
                            }
                        }
                        // `Vars: TLID(-1)` is a unary minus. Inputs already
                        // accept that form; variables must too.
                        let init_value = if init_ty == Some("unaryop") {
                            let value = eval_input_default(init)?;
                            if matches!(value, Value::Num(_)) {
                                self.numeric_vars.insert(name.clone());
                            }
                            value
                        } else {
                            eval_literal(init)?
                        };
                        let slot = self.var_names.len();
                        self.var_slots.insert(name.clone(), slot);
                        self.var_names.push(name.clone());
                        self.var_inits.push(init_value);
                        self.stateful.push(name);
                    }
                }
                Some("array_decl") => {
                    return Err(KernelError::unsupported("array declarations"));
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn collect_double_accums(&mut self, stmts: &[Json]) {
        for stmt in stmts {
            match node_type(stmt) {
                Some("assign") => {
                    if let Some(target) = stmt.get("target") {
                        if node_type(target) == Some("ident") {
                            if let Some(name) = target.get("name").and_then(Json::as_str) {
                                if self.numeric_vars.contains(name)
                                    && ident_referenced(
                                        stmt.get("value").unwrap_or(&Json::Null),
                                        name,
                                    )
                                {
                                    self.double_accum.insert(name.to_string());
                                }
                            }
                        }
                    }
                }
                Some("if") => {
                    if let Some(Json::Array(body)) = stmt.get("body") {
                        self.collect_double_accums(body);
                    }
                    if let Some(Json::Array(body)) = stmt.get("else_body") {
                        self.collect_double_accums(body);
                    }
                }
                Some("once") | Some("for") | Some("while") | Some("repeat_until") => {
                    if let Some(Json::Array(body)) = stmt.get("body") {
                        self.collect_double_accums(body);
                    }
                }
                _ => {}
            }
        }
    }

    // ---- statement compilation ----

    fn compile_stmt(&mut self, stmt: &Json) -> Result<(), KernelError> {
        match node_type(stmt) {
            Some("input_decl") | Some("attribute") => Ok(()),
            Some("var_decl") => {
                for decl in decls_of(stmt)? {
                    let name = decl_name(decl)?.to_string();
                    let slot = self.var_slots[&name];
                    self.instrs.push(Instr::LoadVar(slot));
                }
                Ok(())
            }
            Some("assign") => {
                let target = stmt
                    .get("target")
                    .ok_or_else(|| KernelError::unsupported("assign missing target"))?;
                if node_type(target) != Some("ident") {
                    return Err(KernelError::unsupported(
                        "assignment target is not an ident",
                    ));
                }
                let name = target
                    .get("name")
                    .and_then(Json::as_str)
                    .ok_or_else(|| KernelError::unsupported("assign target missing name"))?;
                let slot = *self.var_slots.get(name).ok_or_else(|| {
                    KernelError::unsupported(format!("assignment to unknown variable {name:?}"))
                })?;
                self.compile_expr(stmt.get("value").unwrap_or(&Json::Null), false)?;
                self.instrs.push(Instr::StoreVar(slot));
                Ok(())
            }
            Some("if") => {
                self.compile_expr(stmt.get("cond").unwrap_or(&Json::Null), false)?;
                let jump_if_false = self.instrs.len();
                self.instrs.push(Instr::JumpIfFalse(0)); // patched below
                if let Some(Json::Array(body)) = stmt.get("body") {
                    for s in body {
                        self.compile_stmt(s)?;
                    }
                }
                let jump_end = self.instrs.len();
                self.instrs.push(Instr::Jump(0)); // patched below
                let else_start = self.instrs.len();
                if let Some(Json::Array(body)) = stmt.get("else_body") {
                    for s in body {
                        self.compile_stmt(s)?;
                    }
                }
                let end = self.instrs.len();
                self.instrs[jump_if_false] = Instr::JumpIfFalse(else_start);
                self.instrs[jump_end] = Instr::Jump(end);
                Ok(())
            }
            Some("once") => {
                let skip = self.instrs.len();
                self.instrs.push(Instr::JumpIfNotFirstBar(0)); // patched below
                if let Some(Json::Array(body)) = stmt.get("body") {
                    for s in body {
                        self.compile_stmt(s)?;
                    }
                }
                self.instrs.push(Instr::SetFirstBarFalse);
                let end = self.instrs.len();
                self.instrs[skip] = Instr::JumpIfNotFirstBar(end);
                Ok(())
            }
            Some("order") => self.compile_order(stmt),
            Some("risk") => self.compile_risk(stmt),
            Some("print") => {
                if let Some(Json::Array(args)) = stmt.get("args") {
                    for arg in args {
                        self.compile_expr(arg, false)?;
                        self.instrs.push(Instr::Drop);
                    }
                }
                Ok(())
            }
            Some("alert") => {
                self.compile_expr(stmt.get("msg").unwrap_or(&Json::Null), false)?;
                self.instrs.push(Instr::Drop);
                Ok(())
            }
            Some("plot") => {
                self.compile_expr(stmt.get("value").unwrap_or(&Json::Null), false)?;
                self.instrs.push(Instr::Drop);
                Ok(())
            }
            Some("commentary") => {
                self.compile_expr(stmt.get("msg").unwrap_or(&Json::Null), false)?;
                self.instrs.push(Instr::Drop);
                Ok(())
            }
            // #BeginCmtry … #End. PowerBridge never turns commentary on, so
            // the block is not executed. Walking it would draw and trade.
            Some("commentary_block") => Ok(()),
            Some("expr_stmt") => {
                self.compile_expr(stmt.get("expr").unwrap_or(&Json::Null), false)?;
                self.instrs.push(Instr::Drop);
                Ok(())
            }
            Some("array_decl") => Err(KernelError::unsupported("array declarations")),
            Some("for") => Err(KernelError::unsupported("for loops")),
            Some("while") => Err(KernelError::unsupported("while loops")),
            Some("repeat_until") => Err(KernelError::unsupported("repeat-until loops")),
            Some("switch") => Err(KernelError::unsupported("switch")),
            Some("break") => Err(KernelError::unsupported("break")),
            Some("continue") => Err(KernelError::unsupported("continue")),
            Some("abort") => Err(KernelError::unsupported("abort")),
            Some(other) => Err(KernelError::unsupported(format!(
                "statement type {other:?}"
            ))),
            None => Err(KernelError::unsupported("statement missing type")),
        }
    }

    fn compile_order(&mut self, stmt: &Json) -> Result<(), KernelError> {
        if stmt.get("entry_label").is_some_and(|v| !v.is_null()) {
            return Err(KernelError::unsupported(
                "from entry(\"…\") named exits are not in the native Order surface",
            ));
        }
        if stmt.get("total").and_then(Json::as_bool) == Some(true) {
            return Err(KernelError::unsupported(
                "order modifier 'total' is not in the native Order surface",
            ));
        }
        let action = match stmt.get("action").and_then(Json::as_str).unwrap_or("") {
            "buy" => Action::Buy,
            "sellshort" => Action::SellShort,
            "sell" => Action::Sell,
            "buytocover" => Action::BuyToCover,
            other => return Err(KernelError::unsupported(format!("order action {other:?}"))),
        };
        let order_type = match stmt.get("order_type").and_then(Json::as_str).unwrap_or("") {
            "market" => OrderType::Market,
            "limit" => OrderType::Limit,
            "stop" => OrderType::Stop,
            "close" => OrderType::Close,
            other => return Err(KernelError::unsupported(format!("order type {other:?}"))),
        };
        let timing = match stmt
            .get("bar_timing")
            .and_then(Json::as_str)
            .unwrap_or("next")
        {
            "next" => Timing::NextBar,
            "this" | "thisbar" => Timing::ThisBar,
            other => return Err(KernelError::unsupported(format!("bar_timing {other:?}"))),
        };
        // quantity
        match stmt.get("quantity") {
            None | Some(Json::Null) => self.instrs.push(Instr::PushNum(0.0)),
            Some(node) => {
                if node_type(node) == Some("ident")
                    && node.get("name").and_then(Json::as_str) == Some("all")
                {
                    self.instrs.push(Instr::PushPositionContracts);
                } else {
                    self.compile_expr(node, false)?;
                }
            }
        }
        // label
        self.compile_label(stmt.get("label").unwrap_or(&Json::Null))?;
        // price
        let has_price = match stmt.get("price") {
            None | Some(Json::Null) => false,
            Some(node) => {
                self.compile_expr(node, false)?;
                true
            }
        };
        self.instrs.push(Instr::EmitOrder {
            action,
            order_type,
            timing,
            has_price,
        });
        Ok(())
    }

    fn compile_label(&mut self, label: &Json) -> Result<(), KernelError> {
        if label.is_null() {
            self.instrs.push(Instr::PushStr(String::new()));
            return Ok(());
        }
        if let Some(s) = label.as_str() {
            self.instrs.push(Instr::PushStr(s.to_string()));
            return Ok(());
        }
        if node_type(label) == Some("string") {
            self.instrs.push(Instr::PushStr(
                label
                    .get("value")
                    .and_then(Json::as_str)
                    .unwrap_or("")
                    .to_string(),
            ));
            return Ok(());
        }
        self.compile_expr(label, false)
    }

    fn compile_risk(&mut self, stmt: &Json) -> Result<(), KernelError> {
        let func = stmt
            .get("func")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("risk node missing func"))?;
        match func {
            "setstoploss" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::StopLoss));
            }
            "setprofittarget" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::ProfitTarget));
            }
            "setbreakeven" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::BreakEven));
            }
            "setstoploss_pt" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::StopLossPt));
            }
            "setprofittarget_pt" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::ProfitTargetPt));
            }
            "setbreakeven_pt" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::BreakEvenPt));
            }
            "setpercenttrailing" | "setpercenttrailing_pt" => {
                let args = stmt
                    .get("args")
                    .and_then(Json::as_array)
                    .ok_or_else(|| KernelError::unsupported("SetPercentTrailing missing args"))?;
                if args.len() < 2 {
                    return Err(KernelError::unsupported(
                        "SetPercentTrailing requires two args",
                    ));
                }
                self.compile_expr(&args[0], false)?;
                self.compile_expr(&args[1], false)?;
                self.instrs
                    .push(Instr::RiskSet(if func == "setpercenttrailing" {
                        RiskOp::PercentTrailing
                    } else {
                        RiskOp::PercentTrailingPt
                    }));
            }
            "setexitonclose" => {
                self.instrs.push(Instr::RiskSet(RiskOp::ExitOnClose));
            }
            "setdollartrailing" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::DollarTrailing));
            }
            "settrailingstop_pt" => {
                self.compile_risk_arg(stmt)?;
                self.instrs.push(Instr::RiskSet(RiskOp::TrailingStopPt));
            }
            "setstopcontract" | "setstopshare" | "setstopposition" => {
                self.instrs.push(Instr::RiskSet(RiskOp::Noop));
            }
            other => {
                return Err(KernelError::unsupported(format!("risk function {other:?}")));
            }
        }
        Ok(())
    }

    fn compile_risk_arg(&mut self, stmt: &Json) -> Result<(), KernelError> {
        match stmt.get("arg") {
            None | Some(Json::Null) => Err(KernelError::unsupported("risk function missing arg")),
            Some(node) => self.compile_expr(node, false),
        }
    }

    // ---- expression compilation ----

    fn compile_expr(&mut self, node: &Json, series_ctx: bool) -> Result<(), KernelError> {
        if node.is_null() {
            return Err(KernelError::unsupported("missing expression"));
        }
        match node_type(node) {
            Some("number") => {
                self.instrs.push(Instr::PushNum(number_literal(node)?));
                Ok(())
            }
            Some("string") => {
                self.instrs.push(Instr::PushStr(
                    node.get("value")
                        .and_then(Json::as_str)
                        .unwrap_or("")
                        .to_string(),
                ));
                Ok(())
            }
            Some("boolean") => {
                self.instrs.push(Instr::PushBool(
                    node.get("value").and_then(Json::as_bool).unwrap_or(false),
                ));
                Ok(())
            }
            Some("ident") => self.compile_ident(node, series_ctx),
            Some("binop") => self.compile_binop(node),
            Some("compare") => self.compile_compare(node),
            Some("unaryop") => self.compile_unary(node),
            Some("call") => self.compile_call(node),
            Some("position_kw") => self.compile_position_kw(node),
            Some("position_kw_call") => Err(KernelError::unsupported(format!(
                "position keyword call {}",
                node.get("kwarg").and_then(Json::as_str).unwrap_or("?")
            ))),
            Some("mc_kw") => self.compile_mc_kw(node),
            Some("bar_ref") => self.compile_bar_ref(node),
            Some("data_of") => self.compile_data_of(node),
            Some(other) => Err(KernelError::unsupported(format!(
                "expression type {other:?}"
            ))),
            None => Err(KernelError::unsupported("expression missing type")),
        }
    }

    fn compile_ident(&mut self, node: &Json, series_ctx: bool) -> Result<(), KernelError> {
        let name = node
            .get("name")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("ident missing name"))?;
        if name == "_market_position" {
            self.instrs.push(Instr::PushMarketPosition);
            return Ok(());
        }
        if let Some(series) = Series::from_name(name) {
            if series_ctx {
                self.instrs.push(Instr::PushSeriesParamSeries(series));
            } else {
                self.instrs.push(Instr::PushSeriesParam(series));
            }
            return Ok(());
        }
        if let Some(slot) = self.var_slots.get(name) {
            if series_ctx {
                self.needs_value_history.insert(name.to_string());
                self.instrs.push(Instr::PushVarSeries(*slot));
            } else {
                self.instrs.push(Instr::PushVar(*slot));
            }
            return Ok(());
        }
        if let Some(slot) = self.input_slots.get(name) {
            self.instrs.push(Instr::PushInput(*slot));
            return Ok(());
        }
        // TickID and ExecOffset matches MultiCharts backtests: 0 on every
        // historical bar (checked against a MultiCharts capture).
        if name == "tickid" || name == "execoffset" {
            self.instrs.push(Instr::PushNum(0.0));
            return Ok(());
        }
        Err(KernelError::unsupported(format!(
            "unresolved identifier {name:?}"
        )))
    }

    fn compile_binop(&mut self, node: &Json) -> Result<(), KernelError> {
        let op = node.get("op").and_then(Json::as_str).unwrap_or("");
        let left = node.get("left").unwrap_or(&Json::Null);
        let right = node.get("right").unwrap_or(&Json::Null);
        match op {
            "and" => {
                self.compile_expr(left, false)?;
                let skip = self.instrs.len();
                self.instrs.push(Instr::JumpIfNotTruthyPeek(0));
                self.instrs.push(Instr::Drop);
                self.compile_expr(right, false)?;
                let end = self.instrs.len();
                self.instrs[skip] = Instr::JumpIfNotTruthyPeek(end);
            }
            "or" => {
                self.compile_expr(left, false)?;
                let skip = self.instrs.len();
                self.instrs.push(Instr::JumpIfTruthyPeek(0));
                self.instrs.push(Instr::Drop);
                self.compile_expr(right, false)?;
                let end = self.instrs.len();
                self.instrs[skip] = Instr::JumpIfTruthyPeek(end);
            }
            _ => {
                self.compile_expr(left, false)?;
                self.compile_expr(right, false)?;
                let binop = match op {
                    "+" => Binop::Add,
                    "-" => Binop::Sub,
                    "*" => Binop::Mul,
                    "/" => Binop::Div,
                    "%" => Binop::Mod,
                    other => {
                        return Err(KernelError::unsupported(format!("binop {other:?}")));
                    }
                };
                self.instrs.push(Instr::Binop(binop));
            }
        }
        Ok(())
    }

    fn compile_compare(&mut self, node: &Json) -> Result<(), KernelError> {
        let op = node.get("op").and_then(Json::as_str).unwrap_or("");
        let cmp = match op {
            "=" | "==" => Cmp::Eq,
            "<>" | "!=" => Cmp::Ne,
            "<" => Cmp::Lt,
            ">" => Cmp::Gt,
            "<=" => Cmp::Le,
            ">=" => Cmp::Ge,
            other => return Err(KernelError::unsupported(format!("compare {other:?}"))),
        };
        self.compile_expr(node.get("left").unwrap_or(&Json::Null), false)?;
        self.compile_expr(node.get("right").unwrap_or(&Json::Null), false)?;
        self.instrs.push(Instr::Compare(cmp));
        Ok(())
    }

    fn compile_unary(&mut self, node: &Json) -> Result<(), KernelError> {
        let op = node.get("op").and_then(Json::as_str).unwrap_or("");
        let unary = match op {
            "not" => Unary::Not,
            "-" => Unary::Neg,
            "+" => Unary::Pos,
            other => return Err(KernelError::unsupported(format!("unaryop {other:?}"))),
        };
        self.compile_expr(node.get("operand").unwrap_or(&Json::Null), false)?;
        self.instrs.push(Instr::Unary(unary));
        Ok(())
    }

    fn compile_position_kw(&mut self, node: &Json) -> Result<(), KernelError> {
        let kw = node
            .get("kwarg")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("position_kw missing kwarg"))?;
        let kind = match kw {
            "barnumber" | "currentbar" => PositionKw::Barnumber,
            "market_position" => PositionKw::MarketPosition,
            "current_contracts" => PositionKw::CurrentContracts,
            "entry_price" => PositionKw::EntryPrice,
            "avgentryprice" => PositionKw::AvgEntryPrice,
            "bars_since_entry" => PositionKw::BarsSinceEntry,
            "exit_price" => PositionKw::ExitPrice,
            "bars_since_exit" => PositionKw::BarsSinceExit,
            "open_position_profit" => PositionKw::OpenPositionProfit,
            "position_profit" => PositionKw::PositionProfit,
            "net_profit" => PositionKw::NetProfit,
            "gross_profit" => PositionKw::GrossProfit,
            "gross_loss" => PositionKw::GrossLoss,
            "totaltrades" => PositionKw::TotalTrades,
            "maxcontractsheld" => PositionKw::MaxContractsHeld,
            "maxiddrawdown" => PositionKw::MaxIdDrawdown,
            "current_date" => PositionKw::CurrentDate,
            "current_time" => PositionKw::CurrentTime,
            "alert_enabled" => PositionKw::AlertEnabled,
            "check_alert" => PositionKw::CheckAlert,
            "lastbaronchartex" => PositionKw::LastBarOnChartEx,
            other => {
                return Err(KernelError::unsupported(format!(
                    "position/data keyword {other:?}"
                )));
            }
        };
        self.instrs.push(Instr::PushPositionKw(kind));
        Ok(())
    }

    fn compile_mc_kw(&mut self, node: &Json) -> Result<(), KernelError> {
        let kw = node
            .get("kwarg")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("mc_kw missing kwarg"))?;
        let kind = match kw {
            "time_s" => McKw::TimeS,
            "bartype_ex" => McKw::BarTypeEx,
            other => return Err(KernelError::unsupported(format!("mc keyword {other:?}"))),
        };
        self.instrs.push(Instr::PushMcKw(kind));
        Ok(())
    }

    fn compile_bar_ref(&mut self, node: &Json) -> Result<(), KernelError> {
        let series = node
            .get("series")
            .ok_or_else(|| KernelError::unsupported("bar_ref missing series"))?;
        self.compile_expr(node.get("index").unwrap_or(&Json::Null), false)?;
        if node_type(series) == Some("position_kw") {
            let kw = series
                .get("kwarg")
                .and_then(Json::as_str)
                .ok_or_else(|| KernelError::unsupported("position_kw missing kwarg"))?;
            let kind = position_kw_kind(kw)
                .ok_or_else(|| KernelError::unsupported(format!("position/data keyword {kw:?}")))?;
            // EntryPrice(n)/ExitPrice(n) with n != 0 read a closed position, which
            // the strategy cannot see here (it gets only PositionView). Refuse so
            // the run falls back to the Python path (pl_pos_ago, issue #9).
            if matches!(kind, PositionKw::EntryPrice | PositionKw::ExitPrice)
                && !is_literal_zero(node.get("index"))
            {
                return Err(KernelError::unsupported(format!(
                    "positions-ago {kw:?}(n) with n != 0"
                )));
            }
            self.instrs.push(Instr::BarRefPositionKw(kind));
            return Ok(());
        }
        if node_type(series) == Some("ident") {
            let name = series.get("name").and_then(Json::as_str).unwrap_or("");
            if let Some(col) = Series::from_name(name) {
                self.instrs.push(Instr::BarRefSeries(col));
                return Ok(());
            }
            if let Some(slot) = self.var_slots.get(name) {
                self.needs_value_history.insert(name.to_string());
                self.instrs.push(Instr::BarRefVar(*slot));
                return Ok(());
            }
        }
        Err(KernelError::unsupported("bar_ref series"))
    }

    fn compile_data_of(&mut self, node: &Json) -> Result<(), KernelError> {
        let series = node
            .get("series")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("data_of missing series"))?;
        let data_num = node
            .get("data_num")
            .and_then(Json::as_i64)
            .ok_or_else(|| KernelError::unsupported("data_of missing data_num"))?;
        if data_num >= 3 {
            return Err(KernelError::unsupported(format!(
                "Data{data_num} series (data_of)"
            )));
        }
        let series = Series::from_name(series).ok_or_else(|| {
            KernelError::unsupported(format!("data series {series:?} is not supported"))
        })?;
        self.instrs.push(Instr::DataOf {
            series,
            data_num: data_num as u8,
        });
        Ok(())
    }

    fn compile_call(&mut self, node: &Json) -> Result<(), KernelError> {
        let name = node
            .get("name")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("call missing name"))?;
        let args_json = node.get("args").and_then(Json::as_array);
        let args: &[Json] = args_json.map(|a| a.as_slice()).unwrap_or(&[]);
        match name {
            "average" | "averagefc" | "averagetfc" => {
                if args.len() != 2 {
                    return Err(KernelError::unsupported("average requires 2 args"));
                }
                let src = self.series_src(&args[0])?;
                self.compile_expr(&args[1], true)?;
                self.instrs.push(Instr::Average(src));
                return Ok(());
            }
            "crossesabove" | "crossesbelow" => {
                if args.len() != 2 {
                    return Err(KernelError::unsupported("crosses requires 2 args"));
                }
                let a = self.series_src(&args[0])?;
                let b = self.series_src(&args[1])?;
                self.instrs.push(Instr::Crosses {
                    above: name == "crossesabove",
                    a,
                    b,
                });
                return Ok(());
            }
            _ => {}
        }
        let (op, _series_fn) = match name {
            "numtostr" => (CallOp::NumToStr, false),
            "file" => (CallOp::File, false),
            "absvalue" | "abs" => (CallOp::AbsValue, false),
            "intportion" => (CallOp::IntPortion, false),
            "sign" => (CallOp::Sign, false),
            "square" => (CallOp::Square, false),
            "maxlist" => (CallOp::MaxList, false),
            "minlist" => (CallOp::MinList, false),
            // Drawing and measured colour calls. Not folded: drawing calls
            // record an object, and the colour calls share one implementation
            // with the interpreter.
            "tl_new" => (CallOp::TlNew, false),
            "tl_setcolor" => (CallOp::TlSetColor, false),
            "tl_setstyle" => (CallOp::TlSetStyle, false),
            "arw_new" => (CallOp::ArwNew, false),
            "text_new" => (CallOp::TextNew, false),
            "rgb" => (CallOp::Rgb, false),
            "legacycolortorgb" => (CallOp::LegacyColorToRgb, false),
            "gradientcolor" => (CallOp::GradientColor, false),
            other => {
                return Err(KernelError::unsupported(format!("function {other:?}")));
            }
        };
        for arg in args {
            self.compile_expr(arg, false)?;
        }
        self.instrs.push(Instr::Call(op, args.len() as u8));
        Ok(())
    }

    /// Resolve a series-function argument (must be a bare ident naming a data
    /// column or a variable), producing a zero-copy [`SeriesSrc`].
    fn series_src(&mut self, node: &Json) -> Result<SeriesSrc, KernelError> {
        if node_type(node) != Some("ident") {
            return Err(KernelError::unsupported("expected a series"));
        }
        let name = node
            .get("name")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("ident missing name"))?;
        if let Some(series) = Series::from_name(name) {
            return Ok(SeriesSrc::Column(series));
        }
        if let Some(slot) = self.var_slots.get(name) {
            if self.numeric_vars.contains(name) {
                self.needs_numeric_history.insert(name.to_string());
                return Ok(SeriesSrc::NumericVar(*slot));
            }
            self.needs_value_history.insert(name.to_string());
            return Ok(SeriesSrc::ValueVar(*slot));
        }
        Err(KernelError::unsupported(format!(
            "unresolved identifier {name:?}"
        )))
    }
}

fn position_kw_kind(kw: &str) -> Option<PositionKw> {
    Some(match kw {
        "barnumber" | "currentbar" => PositionKw::Barnumber,
        "market_position" => PositionKw::MarketPosition,
        "current_contracts" => PositionKw::CurrentContracts,
        "entry_price" => PositionKw::EntryPrice,
        "avgentryprice" => PositionKw::AvgEntryPrice,
        "bars_since_entry" => PositionKw::BarsSinceEntry,
        "exit_price" => PositionKw::ExitPrice,
        "bars_since_exit" => PositionKw::BarsSinceExit,
        "open_position_profit" => PositionKw::OpenPositionProfit,
        "position_profit" => PositionKw::PositionProfit,
        "net_profit" => PositionKw::NetProfit,
        "gross_profit" => PositionKw::GrossProfit,
        "gross_loss" => PositionKw::GrossLoss,
        "totaltrades" => PositionKw::TotalTrades,
        "maxcontractsheld" => PositionKw::MaxContractsHeld,
        "maxiddrawdown" => PositionKw::MaxIdDrawdown,
        "current_date" => PositionKw::CurrentDate,
        "current_time" => PositionKw::CurrentTime,
        "alert_enabled" => PositionKw::AlertEnabled,
        "check_alert" => PositionKw::CheckAlert,
        "lastbaronchartex" => PositionKw::LastBarOnChartEx,
        _ => return None,
    })
}

fn body_of(program: &Json) -> Result<&[Json], KernelError> {
    program
        .get("body")
        .and_then(Json::as_array)
        .map(|a| a.as_slice())
        .ok_or_else(|| KernelError::input("program missing body"))
}

fn decls_of(stmt: &Json) -> Result<&[Json], KernelError> {
    stmt.get("decls")
        .and_then(Json::as_array)
        .map(|a| a.as_slice())
        .ok_or_else(|| KernelError::input("decl node missing decls"))
}

fn decl_name(decl: &Json) -> Result<&str, KernelError> {
    decl.get("name")
        .and_then(Json::as_str)
        .ok_or_else(|| KernelError::input("decl missing name"))
}

/// Look up an Input override.
///
/// EasyLanguage identifiers are case-insensitive. The lexer folds IR `name`
/// fields to lowercase (`myparam`, `fastlen`), while sweep callers pass the
/// source spelling (`MyParam`, `FastLen`). An exact match wins; otherwise a
/// case-insensitive match applies. This is why `{"MyParam": 0.101}` must
/// replace the IR default even though the decl name is `myparam`.
fn lookup_override<'a>(name: &str, overrides: &'a HashMap<String, Value>) -> Option<&'a Value> {
    if let Some(over) = overrides.get(name) {
        return Some(over);
    }
    overrides
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .map(|(_, value)| value)
}

/// Resolve one input's runtime value: an explicit override wins, else a
/// projected runtime input, else the IR default. This is the single place input
/// resolution happens, so a solo run and a batch run resolve byte-identically.
fn resolve_input_value(
    name: &str,
    default: &Json,
    overrides: &HashMap<String, Value>,
) -> Result<Value, KernelError> {
    if let Some(over) = lookup_override(name, overrides) {
        return Ok(over.clone());
    }
    eval_input_default(default)
}

/// Resolve the full ordered input vector (one slot per `input_decl`, in body
/// order) for a set of overrides, WITHOUT recompiling the program.
///
/// This mirrors `collect_decls`' input-decl walk exactly and produces the same
/// slot order, so a batch runner may compile once (default inputs) and swap in
/// the per-variant resolved inputs by cloning the program and replacing
/// `Program::inputs`. An override affects no other part of `Program`.
pub(crate) fn resolve_inputs(
    ast: &Json,
    overrides: &HashMap<String, Value>,
) -> Result<Vec<Value>, KernelError> {
    let body = body_of(ast)?;
    let mut inputs = Vec::new();
    for stmt in body {
        if node_type(stmt) == Some("input_decl") {
            for decl in decls_of(stmt)? {
                let name = decl_name(decl)?.to_string();
                let default = decl
                    .get("default")
                    .ok_or_else(|| KernelError::input(format!("input {name} has no default")))?;
                inputs.push(resolve_input_value(&name, default, overrides)?);
            }
        }
    }
    Ok(inputs)
}

fn eval_input_default(node: &Json) -> Result<Value, KernelError> {
    if node_type(node) != Some("unaryop") {
        return eval_literal(node);
    }
    let operand = eval_input_default(node.get("operand").unwrap_or(&Json::Null))?;
    match node.get("op").and_then(Json::as_str).unwrap_or("") {
        "-" => Ok(Value::Num(-operand.as_f64()?)),
        "+" => Ok(Value::Num(operand.as_f64()?)),
        other => Err(KernelError::unsupported(format!(
            "input-default unary operator {other:?}"
        ))),
    }
}

/// True when `node` is a number literal equal to 0 (e.g. the `0` in `EntryPrice(0)`).
fn is_literal_zero(node: Option<&Json>) -> bool {
    node.is_some_and(|n| node_type(n) == Some("number") && number_literal(n).ok() == Some(0.0))
}

fn number_literal(node: &Json) -> Result<f64, KernelError> {
    let raw = node
        .get("value")
        .and_then(Json::as_str)
        .ok_or_else(|| KernelError::input("number node missing value"))?;
    let cleaned = if raw.contains('.') {
        raw
    } else {
        let trimmed = raw.trim_start_matches('0');
        if trimmed.is_empty() {
            "0"
        } else {
            trimmed
        }
    };
    cleaned
        .parse()
        .map_err(|_| KernelError::input(format!("bad number literal {raw:?}")))
}

fn eval_literal(node: &Json) -> Result<Value, KernelError> {
    match node_type(node) {
        Some("number") => Ok(Value::Num(number_literal(node)?)),
        Some("string") => Ok(Value::str(
            node.get("value").and_then(Json::as_str).unwrap_or(""),
        )),
        Some("boolean") => Ok(Value::Bool(
            node.get("value").and_then(Json::as_bool).unwrap_or(false),
        )),
        other => Err(KernelError::unsupported(format!("literal type {other:?}"))),
    }
}

fn ident_referenced(node: &Json, name: &str) -> bool {
    match node {
        Json::Object(map) => {
            if map.get("type").and_then(Json::as_str) == Some("ident")
                && map.get("name").and_then(Json::as_str) == Some(name)
            {
                return true;
            }
            map.values().any(|child| ident_referenced(child, name))
        }
        Json::Array(items) => items.iter().any(|child| ident_referenced(child, name)),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use serde_json::Value as Json;

    use super::super::value::Value;
    use super::{compile, resolve_inputs, CallOp, Instr, Program};

    fn needs(src: &str) -> (Vec<bool>, Vec<bool>) {
        let ast: Json = serde_json::from_str(src).expect("valid program JSON");
        let program: Program = compile(&ast, &HashMap::<String, Value>::new())
            .unwrap_or_else(|e| panic!("compile failed: {e}"));
        (
            program.var_needs_value_history,
            program.var_needs_numeric_history,
        )
    }

    /// One numeric variable, only ever used as a scalar: neither history is
    /// attached (the reference-strategy shape).
    #[test]
    fn scalar_only_variable_needs_no_history() {
        let (value, numeric) = needs(
            r#"{"type":"program","body":[
                {"type":"var_decl","decls":[{"name":"X","init":{"type":"number","value":"0"}}]},
                {"type":"assign","target":{"type":"ident","name":"X"},
                 "value":{"type":"binop","op":"+","left":{"type":"ident","name":"close"},"right":{"type":"number","value":"1"}}},
                {"type":"if","cond":{"type":"compare","op":">","left":{"type":"ident","name":"X"},"right":{"type":"number","value":"0"}},
                 "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                          "label":{"type":"string","value":"LE"},"quantity":{"type":"number","value":"1"}}],
                 "else_body":null}
            ]}"#,
        );
        assert_eq!(value, vec![false]);
        assert_eq!(numeric, vec![false]);
    }

    fn compile_entry_price_ref(index: &str) -> Result<Program, super::KernelError> {
        let src = format!(
            r#"{{"type":"program","body":[
                {{"type":"var_decl","decls":[{{"name":"X","init":{{"type":"number","value":"0"}}}}]}},
                {{"type":"assign","target":{{"type":"ident","name":"X"}},
                 "value":{{"type":"bar_ref","series":{{"type":"position_kw","kwarg":"entry_price"}},
                          "index":{index}}}}}
            ]}}"#
        );
        let ast: Json = serde_json::from_str(&src).expect("valid program JSON");
        compile(&ast, &HashMap::<String, Value>::new())
    }

    /// `EntryPrice(0)` is the open position and compiles; `EntryPrice(n)` for
    /// n != 0 (or a non-literal n) needs the closed-position ledger and is
    /// refused so the run falls back to Python (issue #9).
    #[test]
    fn positions_ago_entry_price_is_refused_unless_zero() {
        assert!(compile_entry_price_ref(r#"{"type":"number","value":"0"}"#).is_ok());
        for index in [
            r#"{"type":"number","value":"1"}"#,
            r#"{"type":"ident","name":"X"}"#,
        ] {
            let error = match compile_entry_price_ref(index) {
                Ok(_) => panic!("EntryPrice({index}) must be refused"),
                Err(error) => error.to_string(),
            };
            assert!(
                error.contains("positions-ago"),
                "unexpected refusal: {error}"
            );
        }
    }

    /// `X[n]` reads the value history (even for a numeric variable).
    #[test]
    fn bar_ref_variable_needs_value_history() {
        let (value, numeric) = needs(
            r#"{"type":"program","body":[
                {"type":"var_decl","decls":[{"name":"X","init":{"type":"number","value":"0"}}]},
                {"type":"if","cond":{"type":"compare","op":">",
                    "left":{"type":"bar_ref","series":{"type":"ident","name":"X"},"index":{"type":"number","value":"1"}},
                    "right":{"type":"number","value":"0"}},
                 "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                          "label":{"type":"string","value":"LE"},"quantity":{"type":"number","value":"1"}}],
                 "else_body":null}
            ]}"#,
        );
        assert_eq!(value, vec![true]);
        assert_eq!(numeric, vec![false]);
    }

    /// `Average(X, n)` needs the numeric history (and not the value history).
    #[test]
    fn average_variable_needs_numeric_history() {
        let (value, numeric) = needs(
            r#"{"type":"program","body":[
                {"type":"var_decl","decls":[{"name":"X","init":{"type":"number","value":"0"}}]},
                {"type":"assign","target":{"type":"ident","name":"X"},
                 "value":{"type":"call","name":"average","args":[{"type":"ident","name":"X"},{"type":"number","value":"5"}]}}
            ]}"#,
        );
        assert_eq!(value, vec![false]);
        assert_eq!(numeric, vec![true]);
    }

    /// `crosses above (X, close)` marks the variable's numeric history and the
    /// close column stays a column (no variable history beyond the marked var).
    #[test]
    fn crosses_variable_needs_numeric_history() {
        let (value, numeric) = needs(
            r#"{"type":"program","body":[
                {"type":"var_decl","decls":[{"name":"X","init":{"type":"number","value":"0"}}]},
                {"type":"if","cond":{"type":"call","name":"crossesabove",
                    "args":[{"type":"ident","name":"X"},{"type":"ident","name":"close"}]},
                 "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                          "label":{"type":"string","value":"LE"},"quantity":{"type":"number","value":"1"}}],
                 "else_body":null}
            ]}"#,
        );
        assert_eq!(value, vec![false]);
        assert_eq!(numeric, vec![true]);
    }

    /// Source-spelling override keys (`MyParam`, `FastLen`) must resolve against
    /// lexer-folded IR names (`myparam`, `fastlen`). Empty overrides keep defaults.
    #[test]
    fn resolve_inputs_matches_source_spelling_case_insensitively() {
        let ast: Json = serde_json::from_str(
            r#"{
                "type":"program",
                "body":[
                    {"type":"input_decl","decls":[
                        {"name":"myparam","default":{"type":"number","value":"0.10"},"_ident":"MyParam"},
                        {"name":"fastlen","default":{"type":"number","value":"10"},"_ident":"FastLen"}
                    ]}
                ]
            }"#,
        )
        .expect("valid program JSON");

        let defaults = resolve_inputs(&ast, &HashMap::new()).expect("defaults");
        assert_eq!(defaults, vec![Value::Num(0.10), Value::Num(10.0)]);

        let mut source_spelling = HashMap::new();
        source_spelling.insert("MyParam".to_string(), Value::Num(0.101));
        let resolved = resolve_inputs(&ast, &source_spelling).expect("MyParam override");
        assert_eq!(
            resolved[0],
            Value::Num(0.101),
            "MyParam must apply to myparam"
        );
        assert_eq!(
            resolved[1],
            Value::Num(10.0),
            "unmentioned inputs stay default"
        );

        let mut folded = HashMap::new();
        folded.insert("myparam".to_string(), Value::Num(0.101));
        assert_eq!(
            resolve_inputs(&ast, &folded).expect("folded key"),
            resolved,
            "lowercase key and source spelling must resolve identically"
        );

        let mut upper = HashMap::new();
        upper.insert("FASTLEN".to_string(), Value::Num(20.0));
        let resolved_fast = resolve_inputs(&ast, &upper).expect("FASTLEN override");
        assert_eq!(resolved_fast[0], Value::Num(0.10));
        assert_eq!(resolved_fast[1], Value::Num(20.0));
    }

    /// A variable observed both ways (`X[n]` and `Average(X, n)`) needs both.
    #[test]
    fn variable_referenced_both_ways_needs_both_histories() {
        let (value, numeric) = needs(
            r#"{"type":"program","body":[
                {"type":"var_decl","decls":[{"name":"X","init":{"type":"number","value":"0"}}]},
                {"type":"assign","target":{"type":"ident","name":"X"},
                 "value":{"type":"call","name":"average","args":[{"type":"ident","name":"X"},{"type":"number","value":"5"}]}},
                {"type":"if","cond":{"type":"compare","op":">",
                    "left":{"type":"bar_ref","series":{"type":"ident","name":"X"},"index":{"type":"number","value":"1"}},
                    "right":{"type":"number","value":"0"}},
                 "body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next",
                          "label":{"type":"string","value":"LE"},"quantity":{"type":"number","value":"1"}}],
                 "else_body":null}
            ]}"#,
        );
        assert_eq!(value, vec![true]);
        assert_eq!(numeric, vec![true]);
    }

    /// `Vars: TLID(-1)` is a unary minus of a number. It used to
    /// refuse with `var tlid init type Some("unaryop")` before any drawing call.
    #[test]
    fn var_init_unary_minus_is_numeric() {
        let ast: Json = serde_json::from_str(
            r#"{"type":"program","body":[
                {"type":"var_decl","decls":[{"name":"tlid","init":{
                    "type":"unaryop","op":"-","operand":{"type":"number","value":"1"}}}]},
                {"type":"assign","target":{"type":"ident","name":"tlid"},
                 "value":{"type":"number","value":"2"}}
            ]}"#,
        )
        .expect("valid program JSON");
        let program = compile(&ast, &HashMap::new()).expect("unary var init");
        assert_eq!(program.var_inits, vec![Value::Num(-1.0)]);
        assert!(program.var_numeric[0]);
    }

    /// A `#BeginCmtry` body is not compiled. A `TL_New` inside it must not
    /// become a call, and the block itself must not refuse the program.
    #[test]
    fn commentary_block_is_not_compiled() {
        let ast: Json = serde_json::from_str(
            r#"{"type":"program","body":[
                {"type":"commentary_block","body":[
                    {"type":"assign","target":{"type":"ident","name":"tlid"},
                     "value":{"type":"call","name":"tl_new","args":[]}}
                ]},
                {"type":"commentary","cl":true,"msg":{"type":"binop","op":"+",
                    "left":{"type":"string","value":"a"},
                    "right":{"type":"string","value":"b"}}}
            ]}"#,
        )
        .expect("valid program JSON");
        let program = compile(&ast, &HashMap::new()).expect("commentary block");
        assert!(
            !program
                .instrs
                .iter()
                .any(|ins| matches!(ins, Instr::Call(CallOp::TlNew, _))),
            "the skipped block must not record a trendline"
        );
    }
}
