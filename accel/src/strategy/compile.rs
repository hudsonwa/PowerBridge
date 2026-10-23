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

use super::arrays::{python_sum, PySum};
use super::ir::node_type;
use super::trace_types::{self, TraceType};
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
    /// `MinMove` / `PriceScale`: the run's instrument config values.
    MinMove,
    PriceScale,
    /// The open position's entry bar date / time (0 when flat).
    EntryDate,
    EntryTime,
    /// `ExitDate` / `ExitTime` of the current position: always 0, like the
    /// Python run, which never sets those keyword values.
    ExitDate,
    ExitTime,
    /// Most contracts held while the current position is open (0 when flat).
    MaxShares,
}

/// A position keyword with a positions-ago form (`EntryDate(n)`, ...): the
/// keys of the Python runtime's `_POS_AGO_FIELDS`. `n >= 1` reads the n-th
/// most recently closed position.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PosAgoField {
    EntryDate,
    EntryTime,
    EntryPrice,
    ExitDate,
    ExitTime,
    ExitPrice,
    MaxShares,
}

impl PosAgoField {
    fn from_kwarg(kw: &str) -> Option<Self> {
        Some(match kw {
            "entry_date" => Self::EntryDate,
            "entry_time" => Self::EntryTime,
            "entry_price" => Self::EntryPrice,
            "exit_date" => Self::ExitDate,
            "exit_time" => Self::ExitTime,
            "exit_price" => Self::ExitPrice,
            "maxshares" => Self::MaxShares,
            _ => return None,
        })
    }

    /// The bare keyword `n <= 0` reads.
    pub(crate) fn current(self) -> PositionKw {
        match self {
            Self::EntryDate => PositionKw::EntryDate,
            Self::EntryTime => PositionKw::EntryTime,
            Self::EntryPrice => PositionKw::EntryPrice,
            Self::ExitDate => PositionKw::ExitDate,
            Self::ExitTime => PositionKw::ExitTime,
            Self::ExitPrice => PositionKw::ExitPrice,
            Self::MaxShares => PositionKw::MaxShares,
        }
    }
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
    /// `Round(x, n)` (`el_round`) or `Round(x)` (`el_round0`).
    Round,
    /// `Floor(x)`: Python `math.floor(x)`.
    Floor,
    /// One-argument `ELDateToDateTime(d)`: `pl_datetojulian(d)`. The
    /// two-argument form is compiled as its `d * 10000 + t` expression.
    ElDateToDateTime,
    /// A computer-clock word (see [`ClockWord`]); the arguments are evaluated
    /// and ignored, as the Python functions take `*args`.
    Clock(ClockWord),
    MaxList,
    MinList,
    TlNew,
    TlSetColor,
    TlSetStyle,
    ArwNew,
    TextNew,
    TextSetColor,
    Rgb,
    LegacyColorToRgb,
    GradientColor,
    /// A session-template word the Python runtime answers with a constant
    /// EL HHmm time whatever its arguments (`pl_sessionendtime` -> 1600,
    /// `pl_sessionstarttime` -> 1700, ...). See [`session_time`].
    SessionTime(u16),
    /// `CurrentSession(...)`: the ISO weekday (1 = Monday .. 7 = Sunday) of
    /// the bar's date. `bar_loop` passes this as the `currentsession` run
    /// value; the arguments are evaluated and ignored, as in Python.
    CurrentSession,
    /// `StrLen(s)`: Python `len(s)`, the code-point count of a string.
    StrLen,
    /// `FormatDate(fmt, dt)`: see [`super::datefmt`].
    FormatDate,
    /// `FileAppend` / `FileDelete`: Python's stubs write nothing and return 0.
    /// The arguments are still evaluated.
    FileNoop,
}

/// A built-in that reads the computer clock on every call, as the runtime's
/// `pl_currenttime_s` & co. do through `_rt_datetime._clock_now()`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ClockWord {
    /// `CurrentTime_s`: HHMMSS, an int.
    TimeS,
    /// `CurrentDate_s`: YYYMMDD (year - 1900), an int.
    DateS,
    /// `ComputerDateTime`: days since 1899-12-30 plus the day fraction.
    ComputerDateTime,
}

/// The clock word `name` names (the bare word or a call).
pub(crate) fn clock_word(name: &str) -> Option<ClockWord> {
    match name {
        "currenttime_s" => Some(ClockWord::TimeS),
        "currentdate_s" => Some(ClockWord::DateS),
        "computerdatetime" => Some(ClockWord::ComputerDateTime),
        _ => None,
    }
}

/// The constant a session-time word returns on the Python path, copied from
/// `pl_transpiler/runtime/_rt_session.py`. The Python functions take
/// `*args` and ignore them; arguments are still evaluated, as in Python.
pub(crate) fn session_time(name: &str) -> Option<u16> {
    Some(match name {
        "sessionendtime" => 1600,
        "sessionstarttime" => 1700,
        "sessionendtimems" | "sess1endtime" | "sess2endtime" => 1515,
        "sessionstarttimems" | "sess1starttime" | "sess2starttime" => 830,
        "sess1firstbartime" | "sess2firstbartime" => 831,
        // Not times, but the same kind of template constant: the week has
        // five sessions and session 1 ends on day 1.
        "sessioncount" | "sessioncountms" => 5,
        "sessionendday" | "sessionenddayms" => 1,
        _ => return None,
    })
}

/// [`session_time`] for a word written without parentheses. Python lowers
/// only these bare words to a `pl_*()` call; bare `SessionEndTime`,
/// `SessionStartTime` and `Sess1StartTime` read run kwargs instead, so they
/// stay refused.
pub(crate) fn bare_session_time(name: &str) -> Option<u16> {
    match name {
        "sess1endtime" | "sess2endtime" | "sess2starttime" | "sess1firstbartime"
        | "sess2firstbartime" | "sessioncount" | "sessioncountms" | "sessionendday"
        | "sessionenddayms" => session_time(name),
        _ => None,
    }
}

/// A whole-array function. The array is baked into the instruction; the
/// remaining EL arguments are on the stack. Each one copies a Python
/// `pl_*` runtime function (cited at its interpreter arm).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ArrayOp {
    SetMaxIndex,
    GetMaxIndex,
    Sum,
    Highest,
    Lowest,
    Fill,
    SetValRange,
    IndexOf,
    Contains,
    /// Array_Get{Boolean,Float,Integer}Value. `bool_default`: a failed read
    /// returns `False` (Boolean) instead of `0` / `0.0`.
    GetValue {
        bool_default: bool,
    },
    /// Array_Set{Boolean,Float,Integer}Value; `bool_default` as for GetValue.
    SetValue {
        bool_default: bool,
    },
}

impl ArrayOp {
    /// EL arguments after the array itself.
    pub(crate) fn argc(self) -> u8 {
        match self {
            ArrayOp::GetMaxIndex => 0,
            ArrayOp::SetMaxIndex
            | ArrayOp::Fill
            | ArrayOp::IndexOf
            | ArrayOp::Contains
            | ArrayOp::GetValue { .. } => 1,
            ArrayOp::Sum | ArrayOp::Highest | ArrayOp::Lowest | ArrayOp::SetValue { .. } => 2,
            ArrayOp::SetValRange => 3,
        }
    }
}

/// The Python type of a value written into an array element, as far as the
/// compiler can tell. Python 3.12+ `sum()` adds floats with compensation and
/// ints without, so `Array_Sum` and `Average` need it; nothing else reads it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ElemKind {
    /// `int` or `bool`.
    Int,
    Float,
    Unknown,
    /// Write sites only: the value is a bare element read `B[j]`, so it has
    /// that element's type (resolved by the interpreter, never stored).
    Read,
}

/// One `Arrays:` declaration. Elements are kept in EL index order (`A[0]`
/// first); the Python list is the reverse.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ArrayDecl {
    pub(crate) name: String,
    pub(crate) init: Value,
    pub(crate) init_kind: ElemKind,
    pub(crate) len: usize,
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

    /// `kind` is the Python type of the series' elements: Python 3.12+
    /// `sum()` compensates floats.
    Average {
        src: SeriesSrc,
        kind: ElemKind,
    },
    Crosses {
        above: bool,
        a: SeriesSrc,
        b: SeriesSrc,
    },

    BarRefSeries(Series),
    BarRefVar(usize),
    BarRefPositionKw(PositionKw),
    /// Pop the positions-ago index, push that position's field.
    PositionsAgo(PosAgoField),
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
        /// `from entry("label")`: a literal label, see `Order::from_entry`.
        from_entry: Option<String>,
    },
    RiskSet(RiskOp),

    /// `A[i]`: pops the index, pushes the element (`A[-(int(i) + 1)]`).
    ArrayGet(usize),
    /// `A[i] = v`: pops the index, then the value.
    ArraySet {
        slot: usize,
        kind: ElemKind,
    },
    /// A whole-array function: pops `op.argc()` arguments, pushes the result.
    /// `kind` is the type of a value argument it writes.
    ArrayCall {
        op: ArrayOp,
        slot: usize,
        kind: ElemKind,
    },
    /// `Array_Copy(src, si, dst, di, n)`: pops n, di, si; pushes 0.
    ArrayCopy {
        src: usize,
        dst: usize,
    },
    /// `for var = start to/downto end`: pops end, then start. Jumps to
    /// `exit` when the range is empty, else sets the loop counter and `var`.
    ForInit {
        slot: usize,
        lp: usize,
        downto: bool,
        exit: usize,
    },
    /// Steps loop `lp`; jumps back to `body` with the next value, or falls
    /// through when the range is done (`var` keeps the last value).
    ForNext {
        slot: usize,
        lp: usize,
        downto: bool,
        body: usize,
    },

    /// Tombstone used only inside the specialiser; stripped before the bar loop.
    Nop,

    /// Numeric binop with baked RHS (TOS is the left operand). Refuses a zero
    /// divisor (see `pynum`).
    BinopC {
        op: Binop,
        rhs: f64,
    },
    /// Numeric binop with baked LHS (TOS is the right operand). Refuses a zero
    /// divisor (see `pynum`).
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
        kind: ElemKind,
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
        from_entry: Option<String>,
    },
    /// `Sell All` / `Buy All`: quantity from `position.contracts`, baked signal.
    EmitOrderAll {
        action: Action,
        order_type: OrderType,
        timing: Timing,
        signal: String,
        from_entry: Option<String>,
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
    // Per-slot flags: a variable declared inside a block (see
    // [`Compiler::collect_block_decls`]). Python makes it a plain local of the
    // bar function, so it is neither traced nor kept from bar to bar.
    pub(crate) var_block: Vec<bool>,
    // Per-slot flags: which slots must keep a growing bar history because their
    // value is observable as a series (BarRefVar read, average/crosses arg).
    // A variable that is only ever read as a scalar keeps no history at all —
    // the interpreter retains only its current value across bars.
    pub(crate) var_needs_value_history: Vec<bool>,
    pub(crate) var_needs_numeric_history: Vec<bool>,

    // Resolved inputs, indexed by input slot (override > projected > default).
    pub(crate) inputs: Vec<Value>,
    // Input names, indexed by input slot (trace columns).
    pub(crate) input_names: Vec<String>,

    // Declared arrays, indexed by array slot.
    pub(crate) arrays: Vec<ArrayDecl>,
    // Number of `for` loops (each owns one loop-counter slot).
    pub(crate) n_loops: usize,
    // The program can hold a string value (a literal, NewLine, a string call,
    // a string variable or input). Python raises TypeError where the kernel
    // refuses a mixed string/number operation, so such a program keeps every
    // instruction and every bar (no dead-code removal, no prefix skip).
    pub(crate) uses_strings: bool,
}

/// Largest array index the kernel allocates (declared size or
/// `Array_SetMaxIndex`); a larger one is refused rather than allocated.
pub(crate) const MAX_ARRAY_INDEX: i64 = 10_000_000;

pub(crate) fn compile(
    ast: &Json,
    input_overrides: &HashMap<String, Value>,
) -> Result<Program, KernelError> {
    let body = body_of(ast)?.to_vec();
    let mut compiler = Compiler::new();
    compiler.collect_decls(&body, input_overrides)?;
    compiler.collect_block_decls(&body);
    compiler.collect_double_accums(&body);
    compiler.collect_var_kinds(&body, decl_inits(&body)?);
    compiler.compile_body(&body)?;
    Ok(compiler.finish())
}

/// One construct the native compiler refuses: the source line of the
/// enclosing statement (0 when the IR carries none) and the refusal message,
/// exactly as a plain [`compile`] would report it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Refusal {
    pub(crate) line: i64,
    pub(crate) message: String,
}

impl Refusal {
    /// A coarse `(kind, name)` for the refusal: kind is one of `function`,
    /// `statement`, `order`, `variable` or `expression`.
    pub(crate) fn kind_and_name(&self) -> (&'static str, String) {
        let detail = self
            .message
            .strip_prefix("unsupported native construct: ")
            .unwrap_or(&self.message);
        let quoted = detail
            .split('"')
            .nth(1)
            .filter(|_| detail.matches('"').count() >= 2)
            .map(str::to_string);
        let word = |n: usize| {
            detail
                .split_whitespace()
                .nth(n)
                .unwrap_or(detail)
                .to_string()
        };
        let named = |n: usize| quoted.clone().unwrap_or_else(|| word(n));
        let statement = [
            ("while loops", "while"),
            ("repeat-until loops", "repeat_until"),
            ("switch", "switch"),
            ("abort", "abort"),
            ("break outside a for loop", "break"),
            ("continue outside a for loop", "continue"),
        ];
        if let Some((_, name)) = statement.iter().find(|(msg, _)| *msg == detail) {
            return ("statement", (*name).to_string());
        }
        if detail.starts_with("statement type ") {
            ("statement", named(2))
        } else if detail.starts_with("array function ") {
            ("function", word(2))
        } else if detail.starts_with("function ") || detail.starts_with("risk function ") {
            ("function", named(1))
        } else if let Some(rest) = detail.strip_prefix("order modifier '") {
            ("order", rest.split('\'').next().unwrap_or(rest).to_string())
        } else if detail.starts_with("order ") || detail.starts_with("bar_timing ") {
            ("order", named(1))
        } else if detail.starts_with("var ") || detail.starts_with("array ") {
            ("variable", word(1))
        } else if detail.starts_with("Data2-tied variable ") {
            ("variable", word(2))
        } else if detail == "string arrays" {
            ("variable", "string_array".to_string())
        } else {
            ("expression", quoted.unwrap_or_else(|| detail.to_string()))
        }
    }
}

/// The text of a literal order label (a bare string or a `string` node).
fn literal_label(label: &Json) -> Option<String> {
    if let Some(s) = label.as_str() {
        return Some(s.to_string());
    }
    if node_type(label) == Some("string") {
        return label
            .get("value")
            .and_then(Json::as_str)
            .map(str::to_string);
    }
    None
}

/// Every construct [`compile`] would refuse in `ast`, in compile order.
///
/// The compiler keeps going after a refusal: a refused statement is skipped
/// (its nested statements are still checked) and a refused declaration still
/// gets its slot, so later uses are not reported again. The first entry is
/// the refusal a plain [`compile`] returns. Malformed IR (an `input` error)
/// is still returned as an error.
pub(crate) fn explain(
    ast: &Json,
    input_overrides: &HashMap<String, Value>,
) -> Result<Vec<Refusal>, KernelError> {
    explain_for(ast, input_overrides, python_sum())
}

/// [`explain`] as if the running Python's `sum()` were `py_sum`.
fn explain_for(
    ast: &Json,
    input_overrides: &HashMap<String, Value>,
    py_sum: PySum,
) -> Result<Vec<Refusal>, KernelError> {
    let body = body_of(ast)?.to_vec();
    let mut compiler = Compiler::new();
    compiler.py_sum = py_sum;
    compiler.refusals = Some(Vec::new());
    compiler.collect_decls(&body, input_overrides)?;
    compiler.collect_block_decls(&body);
    compiler.collect_double_accums(&body);
    compiler.collect_var_kinds(&body, decl_inits(&body)?);
    compiler.compile_body(&body)?;
    Ok(compiler.refusals.unwrap_or_default())
}

/// Python trace types of `program`'s variables and inputs, in slot order.
/// Only the trace entry point needs them, so a plain compile skips this walk.
pub(crate) fn trace_types(
    ast: &Json,
    program: &Program,
) -> Result<(Vec<TraceType>, Vec<TraceType>), KernelError> {
    let body = body_of(ast)?;
    let double_accum: HashSet<String> = program
        .var_names
        .iter()
        .zip(&program.var_double_accum)
        .filter(|(_, accum)| **accum)
        .map(|(name, _)| name.clone())
        .collect();
    let mut vars = Vec::new();
    let mut inputs = Vec::new();
    for stmt in body {
        match node_type(stmt) {
            Some("var_decl") => {
                for decl in decls_of(stmt)? {
                    vars.push(trace_types::VarDecl {
                        name: decl_name(decl)?,
                        init: decl.get("init").unwrap_or(&Json::Null),
                        block: false,
                    });
                }
            }
            Some("input_decl") => {
                for decl in decls_of(stmt)? {
                    inputs.push(trace_types::InputDecl {
                        name: decl_name(decl)?,
                        default: decl.get("default").unwrap_or(&Json::Null),
                    });
                }
            }
            _ => {}
        }
    }
    // Block-declared variables follow, in slot order.
    for (name, _) in program
        .var_names
        .iter()
        .zip(&program.var_block)
        .filter(|(_, block)| **block)
    {
        vars.push(trace_types::VarDecl {
            name,
            init: &Json::Null,
            block: true,
        });
    }
    Ok(trace_types::infer(body, &vars, &inputs, &double_accum))
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
    input_names: Vec<String>,
    needs_value_history: HashSet<String>,
    needs_numeric_history: HashSet<String>,
    array_slots: HashMap<String, usize>,
    arrays: Vec<ArrayDecl>,
    n_loops: usize,
    uses_strings: bool,
    /// `OutPath` reads in a string position of a file sink (see
    /// [`Compiler::allow_outpath`]); every other read is refused.
    outpath_ok: HashSet<*const Json>,
    /// Pending `break` / `continue` jumps of the enclosing `for` loops.
    loops: Vec<LoopJumps>,
    /// Variables assigned by any `for` statement (their Python type is `int`
    /// after the loop, so they are not plain `float` variables).
    for_vars: HashSet<String>,
    /// Loop variables of the enclosing `for` loops whose body does not assign
    /// them: an `int` from `range()` everywhere inside the body.
    int_loop_vars: Vec<String>,
    /// Inputs whose default is an integer literal (Python binds an `int`).
    int_inputs: HashSet<String>,
    /// Variables that always hold a Python `int` or `bool` (see
    /// [`Compiler::collect_var_kinds`]).
    int_vars: HashSet<String>,
    /// Variables Python seeds and assigns through `f32()`, so always a
    /// `float`: a plain number init, not an accumulator, not a loop variable.
    float_vars: HashSet<String>,
    /// Names declared only inside blocks; each has one non-stateful slot.
    block_vars: HashSet<String>,
    /// The block-declared names in scope at the statement being compiled.
    block_scope: Vec<String>,
    /// Names a call argument or a lookback reads (a superset of the
    /// variables Python keeps a history list for).
    series_refs: HashSet<String>,
    /// Statement-list nesting: 1 at the top level.
    depth: usize,
    /// The running Python's `sum()` (`Average` depends on it).
    py_sum: PySum,
    /// `Some` in [`explain`] mode: refusals are recorded here and compiling
    /// continues. `None` (a plain [`compile`]): the first refusal is returned.
    refusals: Option<Vec<Refusal>>,
    /// Source line of the statement being compiled (`_line`, 0 if absent).
    line: i64,
}

#[derive(Default)]
struct LoopJumps {
    breaks: Vec<usize>,
    continues: Vec<usize>,
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
            input_names: Vec::new(),
            needs_value_history: HashSet::new(),
            needs_numeric_history: HashSet::new(),
            array_slots: HashMap::new(),
            arrays: Vec::new(),
            n_loops: 0,
            uses_strings: false,
            outpath_ok: HashSet::new(),
            loops: Vec::new(),
            for_vars: HashSet::new(),
            int_loop_vars: Vec::new(),
            int_inputs: HashSet::new(),
            int_vars: HashSet::new(),
            float_vars: HashSet::new(),
            block_vars: HashSet::new(),
            block_scope: Vec::new(),
            series_refs: HashSet::new(),
            depth: 0,
            py_sum: python_sum(),
            refusals: None,
            line: 0,
        }
    }

    /// Pass `result` through, or, in [`explain`] mode, record an
    /// `unsupported` refusal at the current line and carry on.
    fn refuse_or(&mut self, result: Result<(), KernelError>) -> Result<(), KernelError> {
        match (result, self.refusals.as_mut()) {
            (Err(error), Some(refusals))
                if !error.to_string().starts_with("invalid accel input:") =>
            {
                refusals.push(Refusal {
                    line: self.line,
                    message: error.to_string(),
                });
                Ok(())
            }
            (result, _) => result,
        }
    }

    /// Set the current line from `stmt`'s `_line`, returning the previous one.
    fn enter_line(&mut self, stmt: &Json) -> i64 {
        let previous = self.line;
        if let Some(line) = stmt.get("_line").and_then(Json::as_i64) {
            self.line = line;
        }
        previous
    }

    /// Compile a statement list. A block-declared name goes out of scope at
    /// the end of the list that declares it.
    fn compile_body(&mut self, stmts: &[Json]) -> Result<(), KernelError> {
        let scope = self.block_scope.len();
        self.depth += 1;
        let result = stmts.iter().try_for_each(|stmt| {
            let previous = self.enter_line(stmt);
            let result = self.compile_stmt(stmt);
            let result = self.refuse_or(result);
            self.line = previous;
            result
        });
        self.depth -= 1;
        self.block_scope.truncate(scope);
        result
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
        let var_block = self
            .var_names
            .iter()
            .map(|name| self.block_vars.contains(name))
            .collect();
        let stateful = self
            .stateful
            .iter()
            .map(|name| self.var_slots[name])
            .collect();
        let uses_strings = self.uses_strings
            || self.var_inits.iter().any(Value::is_str)
            || self.inputs.iter().any(Value::is_str);
        Program {
            instrs: self.instrs,
            var_names: self.var_names,
            var_numeric,
            var_double_accum,
            var_inits: self.var_inits,
            stateful,
            var_block,
            var_needs_value_history,
            var_needs_numeric_history,
            inputs: self.inputs,
            input_names: self.input_names,
            arrays: self.arrays,
            n_loops: self.n_loops,
            uses_strings,
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
                        let unsigned = match node_type(default) {
                            Some("unaryop") => default.get("operand").unwrap_or(&Json::Null),
                            _ => default,
                        };
                        if node_type(unsigned) == Some("number")
                            && literal_kind(unsigned) == ElemKind::Int
                        {
                            self.int_inputs.insert(name.clone());
                        }
                        let slot = self.inputs.len();
                        self.input_names.push(name.clone());
                        self.input_slots.insert(name, slot);
                        self.inputs.push(value);
                    }
                }
                Some("var_decl") => {
                    let previous = self.enter_line(stmt);
                    for decl in decls_of(stmt)? {
                        let result = self.declare_var(decl);
                        if result.is_err() && self.refusals.is_some() {
                            // Keep the slot so later uses are not refused too.
                            let name = decl_name(decl)?.to_string();
                            if !self.var_slots.contains_key(&name) {
                                self.var_slots.insert(name.clone(), self.var_names.len());
                                self.var_names.push(name.clone());
                                self.var_inits.push(Value::Num(0.0));
                                self.stateful.push(name);
                            }
                        }
                        self.refuse_or(result)?;
                    }
                    self.line = previous;
                }
                Some("array_decl") => {
                    let previous = self.enter_line(stmt);
                    for decl in decls_of(stmt)? {
                        let result = self.declare_array(decl);
                        if result.is_err() && self.refusals.is_some() {
                            let name = decl_name(decl)?.to_string();
                            if !self.array_slots.contains_key(&name) {
                                self.array_slots.insert(name.clone(), self.arrays.len());
                                self.arrays.push(ArrayDecl {
                                    name,
                                    init: Value::Num(0.0),
                                    init_kind: ElemKind::Unknown,
                                    len: 1,
                                });
                            }
                        }
                        self.refuse_or(result)?;
                    }
                    self.line = previous;
                }
                _ => {}
            }
        }
        collect_for_vars(body, &mut self.for_vars);
        let clashes: Vec<String> = self
            .array_slots
            .keys()
            .filter(|name| {
                self.var_slots.contains_key(*name)
                    || self.input_slots.contains_key(*name)
                    || Series::from_name(name).is_some()
            })
            .cloned()
            .collect();
        for name in clashes {
            self.refuse_or(Err(KernelError::unsupported(format!(
                "array {name} shares its name with a variable, input or price series"
            ))))?;
        }
        Ok(())
    }

    /// `codegen_stmt.gen_var_decl` for a `Vars:` inside a block: the name is
    /// not one of the codegen's stateful (top-level) variables, so it is a
    /// plain local, `name = <init>` each time the declaration runs, with no
    /// `f32()`, no trace column and nothing kept between bars. Each such
    /// name gets one slot after the top-level ones. A name that is also
    /// declared at the top level, or is an input, array or price series,
    /// gets none (see [`Compiler::compile_block_decl`]).
    fn collect_block_decls(&mut self, body: &[Json]) {
        fn walk(compiler: &mut Compiler, stmts: &[Json], nested: bool) {
            for stmt in stmts {
                if nested && node_type(stmt) == Some("var_decl") {
                    for decl in decls_of(stmt).unwrap_or(&[]) {
                        let Ok(name) = decl_name(decl) else { continue };
                        if compiler.var_slots.contains_key(name)
                            || compiler.input_slots.contains_key(name)
                            || compiler.array_slots.contains_key(name)
                            || Series::from_name(name).is_some()
                        {
                            continue;
                        }
                        compiler
                            .var_slots
                            .insert(name.to_string(), compiler.var_names.len());
                        compiler.var_names.push(name.to_string());
                        compiler.var_inits.push(Value::Num(0.0));
                        compiler.block_vars.insert(name.to_string());
                    }
                }
                for key in ["body", "else_body"] {
                    if let Some(Json::Array(inner)) = stmt.get(key) {
                        walk(compiler, inner, true);
                    }
                }
            }
        }
        walk(self, body, false);
        collect_series_refs(&Json::Array(body.to_vec()), false, &mut self.series_refs);
    }

    /// A `Vars:` inside a block (see [`Compiler::collect_block_decls`]).
    fn compile_block_decl(&mut self, stmt: &Json) -> Result<(), KernelError> {
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
            if !self.block_vars.contains(&name) {
                if !self.var_slots.contains_key(&name) {
                    return Err(KernelError::unsupported(format!(
                        "var {name} declared inside a block shares its name with \
                         an input, array or price series"
                    )));
                }
                // Redeclaring a top-level variable: Python emits nothing for
                // a scalar, but reloads the previous bar's value for one it
                // keeps a history list for. Only the no-op is native.
                let literal = match node_type(init) {
                    Some("number" | "string" | "boolean") => true,
                    Some("unaryop") => {
                        node_type(init.get("operand").unwrap_or(&Json::Null)) == Some("number")
                    }
                    _ => false,
                };
                if !literal || self.series_refs.contains(&name) {
                    return Err(KernelError::unsupported(format!(
                        "var {name} redeclared inside a block (with a non-literal \
                         init, or read as a series or with a lookback)"
                    )));
                }
                continue;
            }
            // Python evaluates the init before binding the name.
            let compiled = self.compile_expr(init, false);
            self.block_scope.push(name.clone());
            compiled?;
            let slot = self.var_slots[&name];
            self.instrs.push(Instr::StoreVar(slot));
        }
        Ok(())
    }

    /// A block-declared variable is a Python local: reading or assigning it
    /// where its declaration has not run on this bar raises in Python
    /// (`UnboundLocalError`), so only uses after the declaration, in the
    /// same block or one nested in it, are native.
    fn check_block_scope(&self, name: &str) -> Result<(), KernelError> {
        if self.block_vars.contains(name) && !self.block_scope.iter().any(|n| n == name) {
            return Err(KernelError::unsupported(format!(
                "var {name} used outside the block that declares it"
            )));
        }
        Ok(())
    }

    /// Read input `name`. Python codegen reads an input as
    /// `kwargs.get(name, default)`, so an input named like a bar-loop kwarg or
    /// a key a runner adds per bar reads the runtime value, not the input.
    /// Such a read is refused, so the run falls back to Python.
    fn push_input(&mut self, name: &str, slot: usize) -> Result<(), KernelError> {
        self.push_input_at(name, slot, None)
    }

    /// [`Self::push_input`] for the identifier `node`. Python reads `OutPath`
    /// as `''` (its sandbox), never the input's value, so the kernel only
    /// reads it where the value cannot matter: a string operand of a file
    /// sink, which is a no-op in both modes. Any other read (a comparison,
    /// arithmetic, a condition or an order quantity) is refused, so the run
    /// falls back to Python and fails or runs the same way.
    fn push_input_at(
        &mut self,
        name: &str,
        slot: usize,
        node: Option<&Json>,
    ) -> Result<(), KernelError> {
        if name == trace_types::BLANKED_INPUT
            && !node.is_some_and(|n| self.outpath_ok.contains(&std::ptr::from_ref(n)))
        {
            return Err(KernelError::unsupported(
                "input 'outpath' is read outside a file path; Python reads OutPath \
                 as '' (its sandbox), not the input",
            ));
        }
        if trace_types::BAR_LOOP_KWARGS.contains(&name)
            || trace_types::PER_BAR_EXTRA_KEYS.contains(&name)
        {
            return Err(KernelError::unsupported(format!(
                "input '{name}' collides with the runtime keyword {name}; \
                 Python reads the runtime value, not the input"
            )));
        }
        self.instrs.push(Instr::PushInput(slot));
        Ok(())
    }

    /// Mark the `OutPath` reads of file-sink argument `arg` that sit in a
    /// string position: `OutPath` itself, or an operand of a `+` chain whose
    /// every operand is a string, so Python's `''` concatenates too.
    fn allow_outpath(&mut self, arg: &Json) {
        if !self.is_string_expr(arg) {
            return;
        }
        match node_type(arg) {
            Some("ident") => {
                if arg.get("name").and_then(Json::as_str) == Some(trace_types::BLANKED_INPUT) {
                    self.outpath_ok.insert(std::ptr::from_ref(arg));
                }
            }
            Some("binop") => {
                self.allow_outpath(arg.get("left").unwrap_or(&Json::Null));
                self.allow_outpath(arg.get("right").unwrap_or(&Json::Null));
            }
            _ => {}
        }
    }

    /// `node` is a string in Python: a string literal, `OutPath`, a variable
    /// with a string init, a string-valued call, or a `+` of strings.
    fn is_string_expr(&self, node: &Json) -> bool {
        match node_type(node) {
            Some("string") => true,
            Some("ident") => {
                let name = node.get("name").and_then(Json::as_str).unwrap_or("");
                name == trace_types::BLANKED_INPUT
                    || self
                        .var_slots
                        .get(name)
                        .is_some_and(|slot| self.var_inits[*slot].is_str())
            }
            Some("call") => matches!(
                node.get("name").and_then(Json::as_str),
                Some("numtostr" | "file" | "formatdate")
            ),
            Some("binop") => {
                node.get("op").and_then(Json::as_str) == Some("+")
                    && self.is_string_expr(node.get("left").unwrap_or(&Json::Null))
                    && self.is_string_expr(node.get("right").unwrap_or(&Json::Null))
            }
            _ => false,
        }
    }

    /// A block-declared variable has no history: Python cannot read it as a
    /// series or with a lookback.
    fn refuse_block_series(&self, name: &str) -> Result<(), KernelError> {
        if self.block_vars.contains(name) {
            return Err(KernelError::unsupported(format!(
                "var {name} declared inside a block is read as a series"
            )));
        }
        Ok(())
    }

    /// One `Vars:` declaration: a literal (or unary-minus) init gets a slot.
    fn declare_var(&mut self, decl: &Json) -> Result<(), KernelError> {
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
        Ok(())
    }

    /// `codegen_stmt.gen_array_decl`: a static `A[N](init)` is
    /// `[init] * (N + 1)`, a dynamic `A[](init)` is `[init]`. Only literal
    /// sizes and numeric / true-false literal inits are native.
    fn declare_array(&mut self, decl: &Json) -> Result<(), KernelError> {
        let name = decl_name(decl)?.to_string();
        let init = decl
            .get("init")
            .ok_or_else(|| KernelError::input(format!("array {name} has no init")))?;
        let (init_value, init_kind) = match node_type(init) {
            Some("string") => return Err(KernelError::unsupported("string arrays")),
            Some("number") => (Value::Num(number_literal(init)?), literal_kind(init)),
            Some("boolean") => (eval_literal(init)?, ElemKind::Int),
            Some("unaryop")
                if node_type(init.get("operand").unwrap_or(&Json::Null)) == Some("number") =>
            {
                (eval_input_default(init)?, literal_kind(&init["operand"]))
            }
            _ => {
                return Err(KernelError::unsupported(format!(
                    "array {name} init is not a number or true/false literal"
                )))
            }
        };
        let len = match decl.get("size") {
            None | Some(Json::Null) => 1,
            Some(size) => {
                let n = int_literal(size).ok_or_else(|| {
                    KernelError::unsupported(format!("array {name} size is not an integer literal"))
                })?;
                if n > MAX_ARRAY_INDEX {
                    return Err(KernelError::unsupported(format!(
                        "array {name} size {n} is above the native limit {MAX_ARRAY_INDEX}"
                    )));
                }
                // `[init] * (N + 1)` is empty for N < 0.
                usize::try_from(n.saturating_add(1)).unwrap_or(0)
            }
        };
        if self.array_slots.contains_key(&name) {
            return Err(KernelError::unsupported(format!(
                "array {name} is declared twice"
            )));
        }
        self.array_slots.insert(name.clone(), self.arrays.len());
        self.arrays.push(ArrayDecl {
            name,
            init: init_value,
            init_kind,
            len,
        });
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

    /// The variables with a known Python type. `float_vars` go through
    /// `f32()`. Python never passes a `true`/`false` variable (seeded `1`/`0`)
    /// or a self-referential accumulator with an integer literal seed through
    /// `f32()`: each keeps the type of what is assigned, so it is an `int` (or
    /// `bool`) when every assignment is, assuming the same of the others;
    /// candidates that fail are dropped until the set is stable.
    fn collect_var_kinds(&mut self, body: &[Json], inits: Vec<(String, Json)>) {
        self.float_vars = inits
            .iter()
            .filter(|(name, init)| {
                node_type(init) == Some("number")
                    && !self.double_accum.contains(name)
                    && !self.for_vars.contains(name)
            })
            .map(|(name, _)| name.clone())
            .collect();
        self.int_vars = inits
            .into_iter()
            .filter(|(name, init)| match node_type(init) {
                Some("boolean") => true,
                Some("number") => {
                    literal_kind(init) == ElemKind::Int && self.double_accum.contains(name)
                }
                _ => false,
            })
            .map(|(name, _)| name)
            .collect();
        loop {
            let mut assigned = Vec::new();
            collect_assignments(body, &mut assigned);
            let failing: Vec<String> = assigned
                .into_iter()
                .filter(|(name, value)| {
                    // Inputs stay out: an override may bind a float.
                    self.int_vars.contains(*name)
                        && !value.is_some_and(|v| self.elem_kind(v) == ElemKind::Int)
                })
                .map(|(name, _)| name.to_string())
                .collect();
            if failing.is_empty() {
                return;
            }
            for name in failing {
                self.int_vars.remove(&name);
            }
        }
    }

    // ---- statement compilation ----

    fn compile_stmt(&mut self, stmt: &Json) -> Result<(), KernelError> {
        match node_type(stmt) {
            Some("input_decl") | Some("attribute") => Ok(()),
            Some("var_decl") if self.depth > 1 => self.compile_block_decl(stmt),
            Some("var_decl") => {
                for decl in decls_of(stmt)? {
                    let name = decl_name(decl)?.to_string();
                    let slot = *self
                        .var_slots
                        .get(&name)
                        .ok_or_else(|| KernelError::input(format!("var {name} has no slot")))?;
                    self.instrs.push(Instr::LoadVar(slot));
                }
                Ok(())
            }
            Some("assign") => {
                let target = stmt
                    .get("target")
                    .ok_or_else(|| KernelError::unsupported("assign missing target"))?;
                if let Some(slot) = self.array_target(target)? {
                    // Python evaluates the value, then the subscript.
                    let value = stmt.get("value").unwrap_or(&Json::Null);
                    let index = target.get("index").unwrap_or(&Json::Null);
                    let mut kind = self.elem_kind(value);
                    if kind == ElemKind::Read && self.reads_array(index) {
                        // The index is evaluated after the value and its
                        // read would be the last one.
                        kind = ElemKind::Unknown;
                    }
                    self.compile_expr(value, false)?;
                    self.compile_array_index(index)?;
                    self.instrs.push(Instr::ArraySet { slot, kind });
                    return Ok(());
                }
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
                self.check_block_scope(name)?;
                self.instrs.push(Instr::StoreVar(slot));
                Ok(())
            }
            Some("if") => {
                let cond = self.compile_expr(stmt.get("cond").unwrap_or(&Json::Null), false);
                self.refuse_or(cond)?;
                let jump_if_false = self.instrs.len();
                self.instrs.push(Instr::JumpIfFalse(0)); // patched below
                if let Some(Json::Array(body)) = stmt.get("body") {
                    self.compile_body(body)?;
                }
                let jump_end = self.instrs.len();
                self.instrs.push(Instr::Jump(0)); // patched below
                let else_start = self.instrs.len();
                if let Some(Json::Array(body)) = stmt.get("else_body") {
                    self.compile_body(body)?;
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
                    self.compile_body(body)?;
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
                        self.allow_outpath(arg);
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
                // The message is not kept, so each extra argument is
                // evaluated and dropped; it is never joined as text.
                if let Some(Json::Array(extra)) = stmt.get("extra_args") {
                    for arg in extra {
                        self.compile_expr(arg, false)?;
                        self.instrs.push(Instr::Drop);
                    }
                }
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
            // Arrays are allocated once, before the first bar (persist).
            Some("array_decl") => Ok(()),
            Some("for") => self.compile_for(stmt),
            Some("while") => Err(KernelError::unsupported("while loops")),
            Some("repeat_until") => Err(KernelError::unsupported("repeat-until loops")),
            Some("switch") => Err(KernelError::unsupported("switch")),
            Some("break") => {
                let at = self.instrs.len();
                self.loops
                    .last_mut()
                    .ok_or_else(|| KernelError::unsupported("break outside a for loop"))?
                    .breaks
                    .push(at);
                self.instrs.push(Instr::Jump(0)); // patched by compile_for
                Ok(())
            }
            Some("continue") => {
                let at = self.instrs.len();
                self.loops
                    .last_mut()
                    .ok_or_else(|| KernelError::unsupported("continue outside a for loop"))?
                    .continues
                    .push(at);
                self.instrs.push(Instr::Jump(0)); // patched by compile_for
                Ok(())
            }
            Some("abort") => Err(KernelError::unsupported("abort")),
            Some(other) => Err(KernelError::unsupported(format!(
                "statement type {other:?}"
            ))),
            None => Err(KernelError::unsupported("statement missing type")),
        }
    }

    /// `codegen_stmt.gen_for`: `for var in range(int(start), int(end) + 1)`
    /// (`int(end) - 1, -1` for downto). Both bounds are evaluated once, the
    /// body cannot change the iteration, and `var` keeps the last value it
    /// was given (unchanged when the range is empty).
    fn compile_for(&mut self, stmt: &Json) -> Result<(), KernelError> {
        let var = stmt
            .get("var")
            .and_then(Json::as_str)
            .ok_or_else(|| KernelError::unsupported("for loop missing var"))?;
        let slot = *self.var_slots.get(var).ok_or_else(|| {
            KernelError::unsupported(format!("for loop over undeclared variable {var:?}"))
        })?;
        self.check_block_scope(var)?;
        let downto = stmt.get("downto").and_then(Json::as_bool).unwrap_or(false);
        let body: &[Json] = stmt
            .get("body")
            .and_then(Json::as_array)
            .map(|a| a.as_slice())
            .unwrap_or(&[]);
        let start = self.compile_expr(stmt.get("start").unwrap_or(&Json::Null), false);
        self.refuse_or(start)?;
        let end = self.compile_expr(stmt.get("end").unwrap_or(&Json::Null), false);
        self.refuse_or(end)?;
        let lp = self.n_loops;
        self.n_loops += 1;
        let init = self.instrs.len();
        self.instrs.push(Instr::ForInit {
            slot,
            lp,
            downto,
            exit: 0, // patched below
        });
        let body_start = self.instrs.len();
        let int_var = !assigns_var(body, var);
        if int_var {
            self.int_loop_vars.push(var.to_string());
        }
        self.loops.push(LoopJumps::default());
        let compiled = self.compile_body(body);
        let jumps = self.loops.pop().unwrap_or_default();
        if int_var {
            self.int_loop_vars.pop();
        }
        compiled?;
        let next = self.instrs.len();
        self.instrs.push(Instr::ForNext {
            slot,
            lp,
            downto,
            body: body_start,
        });
        let end = self.instrs.len();
        self.instrs[init] = Instr::ForInit {
            slot,
            lp,
            downto,
            exit: end,
        };
        for at in jumps.breaks {
            self.instrs[at] = Instr::Jump(end);
        }
        for at in jumps.continues {
            self.instrs[at] = Instr::Jump(next);
        }
        Ok(())
    }

    /// The array slot when `target` is an element `A[i]` of a declared array.
    fn array_target(&self, target: &Json) -> Result<Option<usize>, KernelError> {
        if node_type(target) != Some("bar_ref") {
            return Ok(None);
        }
        let series = target.get("series").unwrap_or(&Json::Null);
        let name = series.get("name").and_then(Json::as_str).unwrap_or("");
        if node_type(series) == Some("ident") {
            if let Some(slot) = self.array_slots.get(name) {
                return Ok(Some(*slot));
            }
        }
        Err(KernelError::unsupported(
            "assignment target is not a variable or an array element",
        ))
    }

    /// The subscript of `A[i]`. A literal index is emitted as written
    /// (`A[-(N + 1)]`), so it must be an integer Python can parse; any other
    /// index is `int(i)` at run time.
    fn compile_array_index(&mut self, index: &Json) -> Result<(), KernelError> {
        if node_type(index) == Some("number") {
            let n = int_literal(index)
                .ok_or_else(|| KernelError::unsupported("array index literal is not an integer"))?;
            self.instrs.push(Instr::PushNum(n as f64));
            return Ok(());
        }
        self.compile_expr(index, false)
    }

    /// The compile-time Python type of `node` when written into an array
    /// (see [`ElemKind`]). `Unknown` whenever it depends on the run.
    fn elem_kind(&self, node: &Json) -> ElemKind {
        use ElemKind::{Float, Int, Unknown};
        let numeric = |a: ElemKind, b: ElemKind| match (a, b) {
            (Int, Int) => Int,
            (Unknown | ElemKind::Read, _) | (_, Unknown | ElemKind::Read) => Unknown,
            _ => Float,
        };
        match node_type(node) {
            Some("number") => literal_kind(node),
            Some("boolean") | Some("compare") => Int,
            Some("ident") => {
                let name = node.get("name").and_then(Json::as_str).unwrap_or("");
                match name {
                    // `_close` etc. are `f32(close[-1])`.
                    "open" | "high" | "low" | "close" | "volume" => Float,
                    "date" | "time" => Int,
                    _ if self.int_loop_vars.iter().any(|v| v == name) => Int,
                    _ if self.int_vars.contains(name) => Int,
                    _ if self.float_vars.contains(name) => Float,
                    _ => Unknown,
                }
            }
            Some("binop") => {
                let left = self.elem_kind(node.get("left").unwrap_or(&Json::Null));
                let right = self.elem_kind(node.get("right").unwrap_or(&Json::Null));
                match node.get("op").and_then(Json::as_str).unwrap_or("") {
                    "+" | "-" | "*" | "%" => numeric(left, right),
                    "/" => match numeric(left, right) {
                        Unknown => Unknown,
                        _ => Float,
                    },
                    _ => Unknown,
                }
            }
            Some("unaryop") => match node.get("op").and_then(Json::as_str).unwrap_or("") {
                "not" => Int,
                _ => match self.elem_kind(node.get("operand").unwrap_or(&Json::Null)) {
                    ElemKind::Read => Unknown,
                    kind => kind,
                },
            },
            Some("call") => match node.get("name").and_then(Json::as_str).unwrap_or("") {
                // pl_array_getmaxindex: len - 1; pl_array_indexof: an index.
                "intportion" | "sign" | "array_getmaxindex" | "array_indexof" => Int,
                "floor" | "currenttime_s" | "currentdate_s" => Int,
                "computerdatetime" => Float,
                "eldatetodatetime" => {
                    match node.get("args").and_then(Json::as_array).map(Vec::as_slice) {
                        Some([_]) => Int,
                        Some([a, b]) => numeric(numeric(self.elem_kind(a), Int), self.elem_kind(b)),
                        _ => Unknown,
                    }
                }
                "average" | "averagefc" | "averagetfc" => Float,
                // el_round0 returns math.floor's int; el_round divides to a float.
                "round" => match node.get("args").and_then(Json::as_array).map(Vec::len) {
                    Some(1) => Int,
                    _ => Float,
                },
                _ => Unknown,
            },
            Some("bar_ref") => {
                let series = node.get("series").unwrap_or(&Json::Null);
                match series.get("name").and_then(Json::as_str) {
                    _ if node_type(series) != Some("ident") => Unknown,
                    Some(name) if self.array_slots.contains_key(name) => ElemKind::Read,
                    Some("open" | "high" | "low" | "close" | "volume") => Float,
                    Some("date" | "time") => Int,
                    _ => Unknown,
                }
            }
            _ => Unknown,
        }
    }

    /// Whether evaluating `node` reads an array element.
    fn reads_array(&self, node: &Json) -> bool {
        match node {
            Json::Object(map) => {
                let is_read = map.get("type").and_then(Json::as_str) == Some("bar_ref")
                    && map
                        .get("series")
                        .and_then(|s| s.get("name"))
                        .and_then(Json::as_str)
                        .is_some_and(|name| self.array_slots.contains_key(name));
                is_read || map.values().any(|child| self.reads_array(child))
            }
            Json::Array(items) => items.iter().any(|child| self.reads_array(child)),
            _ => false,
        }
    }

    /// Whether Python sees `node` as an `int`: slices (`items[start:end + 1]`)
    /// and list repetition (`[0] * n`) raise `TypeError` on a float. An
    /// input counts when its default is an integer literal; the interpreter
    /// still rejects a non-integral value at run time.
    fn is_int_arg(&self, node: &Json) -> bool {
        if node_type(node) == Some("ident") {
            let name = node.get("name").and_then(Json::as_str).unwrap_or("");
            if self.input_slots.contains_key(name) {
                return self.int_inputs.contains(name);
            }
        }
        match node_type(node) {
            Some("binop") => {
                matches!(
                    node.get("op").and_then(Json::as_str),
                    Some("+" | "-" | "*" | "%")
                ) && self.is_int_arg(node.get("left").unwrap_or(&Json::Null))
                    && self.is_int_arg(node.get("right").unwrap_or(&Json::Null))
            }
            Some("unaryop") => {
                matches!(node.get("op").and_then(Json::as_str), Some("-" | "+"))
                    && self.is_int_arg(node.get("operand").unwrap_or(&Json::Null))
            }
            _ => self.elem_kind(node) == ElemKind::Int,
        }
    }

    fn compile_order(&mut self, stmt: &Json) -> Result<(), KernelError> {
        // `total` is parsed but the Python engine never reads it
        // (`engine.py:_check_named_exit` only strips it), so it compiles to
        // the same order as without it.
        let from_entry = match stmt.get("entry_label") {
            None | Some(Json::Null) => None,
            Some(label) => Some(literal_label(label).ok_or_else(|| {
                KernelError::unsupported(
                    "order modifier 'from entry' with a non-literal label is not in the \
                     native Order surface",
                )
            })?),
        };
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
            from_entry,
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
                self.uses_strings = true;
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
        if self.array_slots.contains_key(name) {
            return Err(KernelError::unsupported(format!(
                "array {name} used as a whole outside a native Array_* function"
            )));
        }
        if let Some(series) = Series::from_name(name) {
            if series_ctx {
                self.instrs.push(Instr::PushSeriesParamSeries(series));
            } else {
                self.instrs.push(Instr::PushSeriesParam(series));
            }
            return Ok(());
        }
        if let Some(&slot) = self.var_slots.get(name) {
            self.check_block_scope(name)?;
            if series_ctx {
                self.refuse_block_series(name)?;
                self.needs_value_history.insert(name.to_string());
                self.instrs.push(Instr::PushVarSeries(slot));
            } else {
                self.instrs.push(Instr::PushVar(slot));
            }
            return Ok(());
        }
        if let Some(slot) = self.input_slots.get(name).copied() {
            return self.push_input_at(name, slot, Some(node));
        }
        // TickID and ExecOffset matches MultiCharts backtests: 0 on every
        // historical bar (checked against a MultiCharts capture).
        if name == "tickid" || name == "execoffset" {
            self.instrs.push(Instr::PushNum(0.0));
            return Ok(());
        }
        if let Some(hhmm) = bare_session_time(name) {
            self.instrs.push(Instr::PushNum(f64::from(hhmm)));
            return Ok(());
        }
        // Bare clock words are emitted as calls (`pl_currenttime_s()`).
        if let Some(word) = clock_word(name) {
            self.instrs.push(Instr::Call(CallOp::Clock(word), 0));
            return Ok(());
        }
        // The runtime's module constant `lastbaronchart_s = True`.
        if name == "lastbaronchart_s" {
            self.instrs.push(Instr::PushBool(true));
            return Ok(());
        }
        // The runtime's module constant `newline = "\n"` (LF only). A variable
        // or input of that name was resolved above, as in Python.
        if name == "newline" {
            self.uses_strings = true;
            self.instrs.push(Instr::PushStr("\n".to_string()));
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
        // Bare CurrentSession reads the `currentsession` run value, which
        // bar_loop always sets to the same weekday CurrentSession(...) returns.
        if kw == "currentsession" {
            self.instrs.push(Instr::Call(CallOp::CurrentSession, 0));
            return Ok(());
        }
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
            "minmove" => PositionKw::MinMove,
            "pricescale" => PositionKw::PriceScale,
            "entry_date" => PositionKw::EntryDate,
            "entry_time" => PositionKw::EntryTime,
            "exit_date" => PositionKw::ExitDate,
            "exit_time" => PositionKw::ExitTime,
            "maxshares" => PositionKw::MaxShares,
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
        if node_type(series) == Some("ident") {
            let name = series.get("name").and_then(Json::as_str).unwrap_or("");
            if let Some(slot) = self.array_slots.get(name).copied() {
                self.compile_array_index(node.get("index").unwrap_or(&Json::Null))?;
                self.instrs.push(Instr::ArrayGet(slot));
                return Ok(());
            }
            // Input[n]: an input is constant for the run, so Python codegen
            // emits its value and drops the index. The index is still compiled
            // (then discarded) so a construct the kernel cannot run is refused.
            // This is also how an input named like a position keyword
            // (`Inputs: ExitTime(1555)`) shadows it in bracket form.
            // `OutPath[n]` is refused by `push_input`: Python reads `OutPath`
            // as "" (its sandbox), not the input.
            if let Some(slot) = self.input_slots.get(name).copied() {
                let mark = self.instrs.len();
                self.compile_expr(node.get("index").unwrap_or(&Json::Null), false)?;
                // Dropping the index's instructions is safe: compiling it only
                // records compiler bookkeeping (slots, history and string
                // flags), never a side effect the run depends on.
                self.instrs.truncate(mark);
                return self.push_input(name, slot);
            }
        }
        self.compile_expr(node.get("index").unwrap_or(&Json::Null), false)?;
        if node_type(series) == Some("position_kw") {
            let kw = series
                .get("kwarg")
                .and_then(Json::as_str)
                .ok_or_else(|| KernelError::unsupported("position_kw missing kwarg"))?;
            let kind = position_kw_kind(kw)
                .ok_or_else(|| KernelError::unsupported(format!("position/data keyword {kw:?}")))?;
            // EntryDate(n)/ExitPrice(n)/... with n != 0 read the closed-position
            // ledger (Python pl_pos_ago); other keywords discard the index.
            match PosAgoField::from_kwarg(kw) {
                Some(field) if !is_literal_zero(node.get("index")) => {
                    self.instrs.push(Instr::PositionsAgo(field));
                }
                _ => self.instrs.push(Instr::BarRefPositionKw(kind)),
            }
            return Ok(());
        }
        if node_type(series) == Some("ident") {
            let name = series.get("name").and_then(Json::as_str).unwrap_or("");
            if let Some(col) = Series::from_name(name) {
                self.instrs.push(Instr::BarRefSeries(col));
                return Ok(());
            }
            if let Some(slot) = self.var_slots.get(name) {
                self.refuse_block_series(name)?;
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
                let kind = self.series_kind(&args[0]);
                // Decided here, not when the window is summed, so that
                // `explain` lists it (the run would refuse it anyway).
                if self.py_sum != PySum::Plain && !matches!(kind, ElemKind::Int | ElemKind::Float) {
                    let shown = match name {
                        "averagefc" => "AverageFC",
                        "averagetfc" => "AverageTFC",
                        _ => "Average",
                    };
                    return Err(KernelError::unsupported(format!(
                        "function {shown} over a series whose Python int/float type is not \
                         known natively (Python 3.12+ sums floats with compensation)"
                    )));
                }
                self.compile_expr(&args[1], true)?;
                self.instrs.push(Instr::Average { src, kind });
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
            // EntryName(n) / ExitName(n): Python codegen emits
            // kwargs.get('entry_name' / 'exit_name', ''), a run value no runner
            // sets, so the result is always "". The index is compiled (then
            // discarded) only so a construct the kernel cannot run is refused.
            "entryname" | "exitname" if !args.is_empty() => {
                let mark = self.instrs.len();
                for arg in args {
                    self.compile_expr(arg, false)?;
                }
                self.instrs.truncate(mark);
                self.uses_strings = true;
                self.instrs.push(Instr::PushStr(String::new()));
                return Ok(());
            }
            _ => {}
        }
        if let Some(op) = array_op(name) {
            return self.compile_array_call(name, op, args);
        }
        if name == "array_copy" {
            return self.compile_array_copy(args);
        }
        if NOT_NATIVE_ARRAY_FUNCTIONS.contains(&name) {
            return Err(KernelError::unsupported(format!(
                "array function {name} is not native yet"
            )));
        }
        let (op, _series_fn) = match name {
            "numtostr" => (CallOp::NumToStr, false),
            "file" => (CallOp::File, false),
            "absvalue" | "abs" => (CallOp::AbsValue, false),
            "intportion" => (CallOp::IntPortion, false),
            "sign" => (CallOp::Sign, false),
            "square" => (CallOp::Square, false),
            "round" => (CallOp::Round, false),
            "floor" => (CallOp::Floor, false),
            "eldatetodatetime" if args.len() == 1 => (CallOp::ElDateToDateTime, false),
            // Emitted as `(d * 10000 + t)`.
            "eldatetodatetime" if args.len() == 2 => {
                let product = serde_json::json!({
                    "type": "binop",
                    "op": "*",
                    "left": args[0],
                    "right": {"type": "number", "value": "10000"},
                });
                let sum = serde_json::json!({
                    "type": "binop",
                    "op": "+",
                    "left": product,
                    "right": args[1],
                });
                return self.compile_binop(&sum);
            }
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
            "text_setcolor" => (CallOp::TextSetColor, false),
            "rgb" => (CallOp::Rgb, false),
            "legacycolortorgb" => (CallOp::LegacyColorToRgb, false),
            "gradientcolor" => (CallOp::GradientColor, false),
            "currentsession" => (CallOp::CurrentSession, false),
            // String helpers of a logging section. The file calls are the
            // Python no-op stubs: nothing is written in either mode.
            "strlen" => (CallOp::StrLen, false),
            "formatdate" => (CallOp::FormatDate, false),
            "fileappend" | "filedelete" => (CallOp::FileNoop, false),
            other => match (session_time(other), clock_word(other)) {
                (Some(hhmm), _) => (CallOp::SessionTime(hhmm), false),
                (None, Some(word)) => (CallOp::Clock(word), false),
                (None, None) => {
                    return Err(KernelError::unsupported(format!("function {other:?}")));
                }
            },
        };
        if matches!(
            op,
            CallOp::NumToStr | CallOp::File | CallOp::StrLen | CallOp::FormatDate
        ) {
            self.uses_strings = true;
        }
        if matches!(op, CallOp::File | CallOp::FileNoop) {
            for arg in args {
                self.allow_outpath(arg);
            }
        }
        for arg in args {
            self.compile_expr(arg, false)?;
        }
        self.instrs.push(Instr::Call(op, args.len() as u8));
        Ok(())
    }

    /// The declared array an Array_* function names as its argument `index`.
    fn array_arg(&self, name: &str, args: &[Json], index: usize) -> Result<usize, KernelError> {
        let arg = args.get(index).unwrap_or(&Json::Null);
        let ident = arg.get("name").and_then(Json::as_str).unwrap_or("");
        match self.array_slots.get(ident) {
            Some(slot) if node_type(arg) == Some("ident") => Ok(*slot),
            _ => Err(KernelError::unsupported(format!(
                "{name} argument {} is not a declared array",
                index + 1
            ))),
        }
    }

    fn compile_array_call(
        &mut self,
        name: &str,
        op: ArrayOp,
        args: &[Json],
    ) -> Result<(), KernelError> {
        let argc = usize::from(op.argc());
        if args.len() != argc + 1 {
            return Err(KernelError::unsupported(format!(
                "{name} takes {} arguments, got {}",
                argc + 1,
                args.len()
            )));
        }
        let slot = self.array_arg(name, args, 0)?;
        let rest = &args[1..];
        // Python slices `items[start:end + 1]` and repeats `[0] * n`: both
        // raise TypeError on a float, so these arguments must be ints.
        let int_args: &[usize] = match op {
            ArrayOp::SetMaxIndex => &[0],
            ArrayOp::Sum | ArrayOp::Highest | ArrayOp::Lowest => &[0, 1],
            _ => &[],
        };
        for &i in int_args {
            if !self.is_int_arg(&rest[i]) {
                return Err(KernelError::unsupported(format!(
                    "{name} argument {} is not an int in Python",
                    i + 2
                )));
            }
        }
        let kind = match op {
            ArrayOp::Fill => self.elem_kind(&rest[0]),
            ArrayOp::SetValRange => self.elem_kind(&rest[2]),
            ArrayOp::SetValue { .. } => self.elem_kind(&rest[1]),
            _ => ElemKind::Unknown,
        };
        for arg in rest {
            self.compile_expr(arg, false)?;
        }
        self.instrs.push(Instr::ArrayCall { op, slot, kind });
        Ok(())
    }

    fn compile_array_copy(&mut self, args: &[Json]) -> Result<(), KernelError> {
        if args.len() != 5 {
            return Err(KernelError::unsupported(format!(
                "array_copy takes 5 arguments, got {}",
                args.len()
            )));
        }
        let src = self.array_arg("array_copy", args, 0)?;
        let dst = self.array_arg("array_copy", args, 2)?;
        for arg in [&args[1], &args[3], &args[4]] {
            self.compile_expr(arg, false)?;
        }
        self.instrs.push(Instr::ArrayCopy { src, dst });
        Ok(())
    }

    /// The Python type of every element of the series named by `node`.
    /// Price columns hold floats and date/time ints; a variable with a
    /// known type holds that type on every bar (a numeric seed goes through
    /// `f32()`); anything else is `Unknown`.
    fn series_kind(&self, node: &Json) -> ElemKind {
        match self.elem_kind(node) {
            ElemKind::Read => ElemKind::Unknown,
            kind => kind,
        }
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
        if self.array_slots.contains_key(name) {
            return Err(KernelError::unsupported(format!(
                "array {name} used as a series argument"
            )));
        }
        if let Some(slot) = self.var_slots.get(name) {
            self.refuse_block_series(name)?;
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

/// Array functions the Python runtime has but the kernel does not run yet.
const NOT_NATIVE_ARRAY_FUNCTIONS: [&str; 13] = [
    "array_gettype",
    "array_sort",
    "sortarray",
    "variancearray",
    "array_compare",
    "array_getstringvalue",
    "array_setstringvalue",
    "arraysize",
    "arraystartaddr",
    "sumlist",
    "avglist",
    "maxlist2",
    "minlist2",
];

fn array_op(name: &str) -> Option<ArrayOp> {
    Some(match name {
        "array_setmaxindex" => ArrayOp::SetMaxIndex,
        "array_getmaxindex" => ArrayOp::GetMaxIndex,
        "array_sum" => ArrayOp::Sum,
        "array_highest" => ArrayOp::Highest,
        "array_lowest" => ArrayOp::Lowest,
        "fill_array" => ArrayOp::Fill,
        "array_setvalrange" => ArrayOp::SetValRange,
        "array_indexof" => ArrayOp::IndexOf,
        "array_contains" => ArrayOp::Contains,
        "array_getbooleanvalue" => ArrayOp::GetValue { bool_default: true },
        "array_getfloatvalue" | "array_getintegervalue" => ArrayOp::GetValue {
            bool_default: false,
        },
        "array_setbooleanvalue" => ArrayOp::SetValue { bool_default: true },
        "array_setfloatvalue" | "array_setintegervalue" => ArrayOp::SetValue {
            bool_default: false,
        },
        _ => return None,
    })
}

/// Python type of a number literal: the codegen emits its text, so a `.` or
/// an exponent makes a `float`.
fn literal_kind(node: &Json) -> ElemKind {
    let raw = node.get("value").and_then(Json::as_str).unwrap_or("");
    if raw.contains(['.', 'e', 'E']) {
        ElemKind::Float
    } else {
        ElemKind::Int
    }
}

/// An integer literal (optionally signed) as Python reads the emitted text.
/// `07` is a Python syntax error and `2.0` is not an int, so both are `None`.
fn int_literal(node: &Json) -> Option<i64> {
    match node_type(node) {
        Some("number") => {
            let raw = node.get("value").and_then(Json::as_str)?;
            if raw.is_empty()
                || !raw.bytes().all(|b| b.is_ascii_digit())
                || (raw.len() > 1 && raw.starts_with('0'))
            {
                return None;
            }
            raw.parse().ok()
        }
        Some("unaryop") => {
            let n = int_literal(node.get("operand")?)?;
            match node.get("op").and_then(Json::as_str)? {
                "-" => n.checked_neg(),
                "+" => Some(n),
                _ => None,
            }
        }
        _ => None,
    }
}

/// `(name, init)` of every declared variable.
fn decl_inits(body: &[Json]) -> Result<Vec<(String, Json)>, KernelError> {
    let mut out = Vec::new();
    for stmt in body {
        if node_type(stmt) == Some("var_decl") {
            for decl in decls_of(stmt)? {
                let init = decl.get("init").cloned().unwrap_or(Json::Null);
                out.push((decl_name(decl)?.to_string(), init));
            }
        }
    }
    Ok(out)
}

/// Every assignment to a plain variable, at any depth, as `(name, value)`.
/// (A `for` loop also writes its variable, always an `int`.)
fn collect_assignments<'a>(stmts: &'a [Json], out: &mut Vec<(&'a str, Option<&'a Json>)>) {
    for stmt in stmts {
        if node_type(stmt) == Some("assign") {
            let target = stmt.get("target").unwrap_or(&Json::Null);
            if node_type(target) == Some("ident") {
                if let Some(name) = target.get("name").and_then(Json::as_str) {
                    out.push((name, stmt.get("value")));
                }
            }
        }
        for key in ["body", "else_body"] {
            if let Some(Json::Array(body)) = stmt.get(key) {
                collect_assignments(body, out);
            }
        }
    }
}

/// Names of every `for` loop variable in `stmts` (any depth).
fn collect_for_vars(stmts: &[Json], out: &mut HashSet<String>) {
    for stmt in stmts {
        if node_type(stmt) == Some("for") {
            if let Some(var) = stmt.get("var").and_then(Json::as_str) {
                out.insert(var.to_string());
            }
        }
        for key in ["body", "else_body"] {
            if let Some(Json::Array(body)) = stmt.get(key) {
                collect_for_vars(body, out);
            }
        }
    }
}

/// Whether `stmts` assign `name` (a plain assignment or a nested `for`).
fn assigns_var(stmts: &[Json], name: &str) -> bool {
    stmts.iter().any(|stmt| {
        let assigned = match node_type(stmt) {
            Some("assign") => {
                let target = stmt.get("target").unwrap_or(&Json::Null);
                node_type(target) == Some("ident")
                    && target.get("name").and_then(Json::as_str) == Some(name)
            }
            Some("for") => stmt.get("var").and_then(Json::as_str) == Some(name),
            _ => false,
        };
        assigned
            || ["body", "else_body"]
                .iter()
                .any(|key| match stmt.get(*key) {
                    Some(Json::Array(body)) => assigns_var(body, name),
                    _ => false,
                })
    })
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
        "entry_date" => PositionKw::EntryDate,
        "entry_time" => PositionKw::EntryTime,
        "exit_date" => PositionKw::ExitDate,
        "exit_time" => PositionKw::ExitTime,
        "maxshares" => PositionKw::MaxShares,
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
pub(crate) fn is_literal_zero(node: Option<&Json>) -> bool {
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

/// `codegen_expr._SERIES_FUNCS`: calls whose arguments the codegen reads as
/// series.
const SERIES_FUNCS: [&str; 33] = [
    "average",
    "averagefc",
    "averagetfc",
    "weightedaverage",
    "xaverage",
    "highest",
    "lowest",
    "highestbar",
    "lowestbar",
    "rsi",
    "momentum",
    "summation",
    "standarddev",
    "stddev",
    "variance",
    "correlation",
    "linearregvalue",
    "linearregslope",
    "macdvalue",
    "macd",
    "macddiff",
    "macdsignal",
    "roc",
    "rateofchange",
    "crossesabove",
    "crossesbelow",
    "waverage",
    "countif",
    "bollingerband",
    "stochastic",
    "linearregangle",
    "summationif",
    "xaveragefc",
];

/// `codegen._collect_history_vars`, widened: every name read anywhere in the
/// arguments of a series function (or of a call nested in one), or as a
/// lookback series with any index.
fn collect_series_refs(node: &Json, in_series: bool, out: &mut HashSet<String>) {
    match node {
        Json::Object(map) => {
            let ty = map.get("type").and_then(Json::as_str);
            if ty == Some("ident") && in_series {
                if let Some(name) = map.get("name").and_then(Json::as_str) {
                    out.insert(name.to_string());
                }
            }
            if ty == Some("bar_ref") {
                if let Some(name) = map
                    .get("series")
                    .filter(|s| node_type(s) == Some("ident"))
                    .and_then(|s| s.get("name"))
                    .and_then(Json::as_str)
                {
                    out.insert(name.to_string());
                }
            }
            let in_series = in_series
                || (ty == Some("call")
                    && map
                        .get("name")
                        .and_then(Json::as_str)
                        .is_some_and(|n| SERIES_FUNCS.contains(&n.to_lowercase().as_str())));
            for child in map.values() {
                collect_series_refs(child, in_series, out);
            }
        }
        Json::Array(items) => {
            for child in items {
                collect_series_refs(child, in_series, out);
            }
        }
        _ => {}
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
    use super::{
        compile, explain, explain_for, resolve_inputs, CallOp, Instr, PosAgoField, PositionKw,
        Program, PySum,
    };

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

    /// `explain` keeps going after a refusal and tags each with its
    /// statement's `_line`; its first message is the one `compile` returns.
    #[test]
    fn explain_lists_every_refusal_in_order() {
        let ast: Json = serde_json::from_str(
            r#"{"type":"program","body":[
                {"type":"var_decl","_line":1,"decls":[{"name":"x","init":{"type":"number","value":"0"}}]},
                {"type":"while","_line":2,"cond":{"type":"number","value":"1"},"body":[]},
                {"type":"if","_line":3,"cond":{"type":"number","value":"1"},"body":[
                    {"type":"switch","_line":4}
                ]},
                {"type":"abort","_line":5}
            ]}"#,
        )
        .expect("valid program JSON");
        let none = HashMap::<String, Value>::new();
        let refusals = explain(&ast, &none).expect("well-formed IR");
        let got: Vec<_> = refusals
            .iter()
            .map(|r| {
                let (kind, name) = r.kind_and_name();
                (kind, name, r.line)
            })
            .collect();
        assert_eq!(
            got,
            [
                ("statement", "while".to_string(), 2),
                ("statement", "switch".to_string(), 4),
                ("statement", "abort".to_string(), 5),
            ]
        );
        let Err(first) = compile(&ast, &none) else {
            panic!("compile must refuse the program");
        };
        assert_eq!(first.to_string(), refusals[0].message);
    }

    /// `total` compiles to the plain order; `from entry("X")` carries its
    /// literal label; a computed label is refused naming the modifier.
    #[test]
    fn order_modifiers_compile_or_name_the_refusal() {
        let order = |extra: &str| {
            format!(
                r#"{{"type":"program","body":[{{"type":"order","_line":1,"action":"sell",
                "order_type":"market","bar_timing":"next","label":"X","quantity":null{extra}}}]}}"#
            )
        };
        let none = HashMap::<String, Value>::new();
        let emitted = |src: String| {
            let ast: Json = serde_json::from_str(&src).expect("valid program JSON");
            let program = compile(&ast, &none).expect("compiles natively");
            program
                .instrs
                .iter()
                .find_map(|i| match i {
                    Instr::EmitOrder { from_entry, .. } => Some(from_entry.clone()),
                    _ => None,
                })
                .expect("one order")
        };
        assert_eq!(emitted(order(r#","total":true"#)), None);
        assert_eq!(
            emitted(order(r#","entry_label":{"type":"string","value":"L"}"#)),
            Some("L".to_string())
        );
        let ast: Json =
            serde_json::from_str(&order(r#","entry_label":{"type":"ident","name":"n"}"#))
                .expect("valid program JSON");
        let refusals = explain(&ast, &none).expect("well-formed IR");
        assert_eq!(refusals.len(), 1);
        assert_eq!(
            refusals[0].kind_and_name(),
            ("order", "from entry".to_string())
        );
    }

    fn ast(json: &str) -> Json {
        serde_json::from_str(json).expect("valid program JSON")
    }

    fn num(v: &str) -> String {
        format!(r#"{{"type":"number","value":"{v}"}}"#)
    }

    fn ident(n: &str) -> String {
        format!(r#"{{"type":"ident","name":"{n}"}}"#)
    }

    fn decl(line: i64, name: &str, init: &str) -> String {
        format!(
            r#"{{"type":"var_decl","_line":{line},"decls":[{{"name":"{name}","init":{init}}}]}}"#
        )
    }

    fn assign(line: i64, name: &str, value: &str) -> String {
        format!(
            r#"{{"type":"assign","_line":{line},"target":{},"value":{value}}}"#,
            ident(name)
        )
    }

    fn if_block(line: i64, body: &[String], else_body: &[String]) -> String {
        format!(
            r#"{{"type":"if","_line":{line},"cond":{},"body":[{}],"else_body":[{}]}}"#,
            num("1"),
            body.join(","),
            else_body.join(",")
        )
    }

    fn program(body: &[String]) -> Json {
        ast(&format!(
            r#"{{"type":"program","body":[{}]}}"#,
            body.join(",")
        ))
    }

    /// A `Vars:` inside a block is a plain local: one slot after the
    /// top-level ones, not stateful, not numeric (no `f32()`), and its
    /// declaration stores the init each time it runs.
    #[test]
    fn block_var_decl_gets_a_local_slot_and_stores_its_init() {
        let body = [
            decl(1, "a", &num("0")),
            if_block(
                2,
                &[decl(3, "z", &num("1.5")), assign(4, "a", &ident("z"))],
                &[decl(5, "z", &num("2")), assign(6, "z", &ident("a"))],
            ),
        ];
        let program = compile(&program(&body), &HashMap::new()).expect("block vars compile");
        assert_eq!(program.var_names, ["a", "z"]);
        assert_eq!(program.var_block, [false, true]);
        assert_eq!(program.stateful, [0]);
        assert_eq!(program.var_numeric, [true, false]);
        let stores = program
            .instrs
            .iter()
            .filter(|i| matches!(i, Instr::StoreVar(1)))
            .count();
        assert_eq!(stores, 3, "two declarations and one assignment");
    }

    /// Uses that Python cannot run (an unset local, a lookback or series
    /// read of a local, a redeclared top-level variable Python keeps a
    /// history for) are refused with a `variable` kind; nothing panics.
    #[test]
    fn block_var_uses_python_cannot_run_are_refused() {
        let lookback = r#"{"type":"bar_ref","series":{"type":"ident","name":"z"},"index":{"type":"number","value":"1"}}"#;
        let average = r#"{"type":"call","name":"average","args":[{"type":"ident","name":"b"},{"type":"number","value":"3"}]}"#;
        let body = [
            decl(1, "a", &num("0")),
            decl(1, "b", &num("0")),
            assign(2, "a", &ident("z")),
            if_block(
                3,
                &[
                    assign(4, "a", &ident("z")),
                    decl(5, "z", &num("0")),
                    assign(6, "a", lookback),
                    decl(7, "b", &num("1")),
                ],
                &[assign(8, "z", &num("1"))],
            ),
            assign(9, "a", average),
        ];
        let none = HashMap::<String, Value>::new();
        let refusals = explain(&program(&body), &none).expect("well-formed IR");
        let got: Vec<(i64, (&str, String))> = refusals
            .iter()
            .map(|r| (r.line, r.kind_and_name()))
            .collect();
        let var = |n: &str| ("variable", n.to_string());
        assert_eq!(
            got,
            [
                (2, var("z")),
                (4, var("z")),
                (6, var("z")),
                (7, var("b")),
                (8, var("z")),
            ]
        );
        assert!(refusals[0]
            .message
            .contains("outside the block that declares it"));
        assert!(refusals[2].message.contains("read as a series"));
        assert!(refusals[3].message.contains("redeclared inside a block"));
        assert!(compile(&program(&body), &none).is_err());
    }

    /// Redeclaring a top-level scalar inside a block is a no-op in Python,
    /// and nothing is emitted for it natively.
    #[test]
    fn redeclared_top_level_scalar_is_a_no_op() {
        let plain = [decl(1, "a", &num("0")), assign(2, "a", &num("3"))];
        let redeclared = [
            decl(1, "a", &num("0")),
            if_block(2, &[decl(3, "a", &num("5"))], &[]),
            assign(4, "a", &num("3")),
        ];
        let none = HashMap::new();
        let with = compile(&program(&redeclared), &none).expect("compiles");
        let without = compile(&program(&plain), &none).expect("compiles");
        assert_eq!(with.var_names, ["a"]);
        assert_eq!(with.var_block, [false]);
        assert!(!with
            .instrs
            .iter()
            .any(|i| matches!(i, Instr::PushNum(n) if *n == 5.0)));
        assert_eq!(
            with.instrs.len(),
            without.instrs.len() + 3,
            "only the if's jumps"
        );
    }

    /// A block var clashing with an input, an array or a price column has
    /// no slot and is refused where it is declared.
    #[test]
    fn block_var_named_like_an_input_is_refused() {
        let ir = ast(&format!(
            r#"{{"type":"program","body":[
                {{"type":"input_decl","_line":1,"decls":[{{"name":"len","default":{}}}]}},
                {}
            ]}}"#,
            num("3"),
            if_block(2, &[decl(3, "len", &num("0"))], &[])
        ));
        let refusals = explain(&ir, &HashMap::new()).expect("well-formed IR");
        assert_eq!(refusals.len(), 1);
        assert_eq!(refusals[0].line, 3);
        assert_eq!(refusals[0].kind_and_name(), ("variable", "len".to_string()));
    }

    /// From Python 3.12 an `Average` over a series of unknown int/float
    /// type is refused at compile time, so `explain` lists it; before 3.12
    /// it compiles.
    #[test]
    fn average_of_unknown_type_is_refused_at_compile_time_from_312() {
        let average = r#"{"type":"call","name":"average","args":[{"type":"ident","name":"x"},{"type":"number","value":"3"}]}"#;
        let half = r#"{"type":"binop","op":"+","left":{"type":"ident","name":"x"},"right":{"type":"number","value":"0.5"}}"#;
        let body = [
            decl(1, "x", &num("0")),
            decl(1, "m", &num("0")),
            assign(2, "x", half),
            assign(3, "m", average),
        ];
        let ir = program(&body);
        let none = HashMap::<String, Value>::new();
        assert_eq!(explain_for(&ir, &none, PySum::Plain).expect("IR"), []);
        for mode in [PySum::CompensatedFloats, PySum::CompensatedAll] {
            let refusals = explain_for(&ir, &none, mode).expect("IR");
            assert_eq!(refusals.len(), 1);
            assert_eq!(refusals[0].line, 3);
            assert_eq!(
                refusals[0].kind_and_name(),
                ("function", "Average".to_string())
            );
            assert!(refusals[0].message.contains("Average over a series"));
        }
        // A known element type (a plain numeric variable is f32()'d: float).
        let known = [
            decl(1, "y", &num("0")),
            decl(1, "m", &num("0")),
            assign(2, "y", &ident("close")),
            assign(3, "m", &average.replace("\"x\"", "\"y\"")),
        ];
        assert_eq!(
            explain_for(&program(&known), &none, PySum::CompensatedAll).expect("IR"),
            []
        );
    }

    /// `EntryPrice(0)` is the bare keyword; `EntryPrice(n)` for n != 0 (or a
    /// non-literal n) reads the closed-position ledger (#149).
    #[test]
    fn positions_ago_entry_price_reads_the_ledger_unless_zero() {
        let has = |index: &str, want: Instr| {
            let program = compile_entry_price_ref(index).expect("native");
            assert!(
                program.instrs.contains(&want),
                "{index}: {:?}",
                program.instrs
            );
        };
        has(
            r#"{"type":"number","value":"0"}"#,
            Instr::BarRefPositionKw(PositionKw::EntryPrice),
        );
        for index in [
            r#"{"type":"number","value":"1"}"#,
            r#"{"type":"ident","name":"X"}"#,
        ] {
            has(index, Instr::PositionsAgo(PosAgoField::EntryPrice));
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
                {"type":"commentary","cl":true,"msg":{"type":"string","value":"a"},
                    "extra_args":[{"type":"number","value":"1"}]}
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
