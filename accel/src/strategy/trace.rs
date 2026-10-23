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

//! Typed per-bar trace — the `run_program_trace` sink contract.
//!
//! The trace sink emits **typed** cells (not the pre-formatted `String` cells of
//! [`crate::engine::report::format_trace_row`]): Rust returns the raw `f64` /
//! `i64` / `bool` / `String` values in the kernel's registers, and Python owns
//! text rendering (`str(value)`). This is the decision the reduced-trace gate
//! ("do not bridge the existing sink as-is") directs: the 24-column
//! text CSV is the *consumer's* formatting problem, not the kernel's.
//!
//! The row set is the **full** per-bar trace (one row per evaluated bar), not
//! the transition-only `audit-event-v1` packet — that packet remains the
//! measured sub-second path; this bridge is the general, reference surface that
//! reproduces the Python `run_el` trace shape but typed.
//!
//! Each cell is the Python value `run_el` would hold: inputs are columns, every
//! store is kept (no dead-store elimination, no skipped bars), and a variable's
//! Python type comes from [`super::trace_types`], so `str()` of a cell is the
//! Python trace's text.

use std::collections::BTreeMap;

use crate::types::{Bar, KernelError, PositionView};

use super::compile::Program;
use super::interp::Interpreter;
use super::trace_types::{TraceKind, TraceType};
use super::value::{NonNumeric, Value};

/// One typed trace cell. `Num` is a Python `float`, `Int` a Python `int`,
/// `Bool` a Python `bool`, `Str` a Python `str`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Cell {
    Num(f64),
    Int(i64),
    Bool(bool),
    Str(String),
}

/// The stable typed trace surface: `columns` are the sorted column names
/// emitted by the first bar, `rows` one typed row per emitted bar. This is the
/// typed counterpart of `crate::types::TraceReport`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct TypedTrace {
    pub(crate) columns: Vec<String>,
    pub(crate) rows: Vec<Vec<Cell>>,
}

/// The engine-owned columns `report.rs::inject_engine_values` (emit_gta6=false)
/// injects, in the same non-underscore spelling Python's `inject_trace` uses.
/// Integer-valued position fields stay integers; money/price fields stay `f64`.
const ENGINE_COLUMNS: [(&str, EngineCell); 15] = [
    ("_market_position", EngineCell::MarketPosition),
    ("current_contracts", EngineCell::Contracts),
    ("entry_price", EngineCell::EntryPrice),
    ("avgentryprice", EngineCell::AvgEntryPrice),
    ("bars_since_entry", EngineCell::BarsSinceEntry),
    ("exit_price", EngineCell::ExitPrice),
    ("bars_since_exit", EngineCell::BarsSinceExit),
    ("open_position_profit", EngineCell::OpenPositionProfit),
    ("position_profit", EngineCell::PositionProfit),
    ("net_profit", EngineCell::NetProfit),
    ("gross_profit", EngineCell::GrossProfit),
    ("gross_loss", EngineCell::GrossLoss),
    ("totaltrades", EngineCell::TotalTrades),
    ("maxcontractsheld", EngineCell::MaxContractsHeld),
    ("maxiddrawdown", EngineCell::MaxIdDrawdown),
];

#[derive(Clone, Copy)]
enum EngineCell {
    MarketPosition,
    Contracts,
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
}

fn engine_cell(kind: EngineCell, position: &PositionView) -> Cell {
    match kind {
        EngineCell::MarketPosition => Cell::Int(position.market_position as i64),
        EngineCell::Contracts => Cell::Int(position.contracts as i64),
        EngineCell::EntryPrice => Cell::Num(position.entry_price),
        EngineCell::AvgEntryPrice => Cell::Num(position.avg_entry_price),
        EngineCell::BarsSinceEntry => Cell::Int(position.bars_since_entry as i64),
        EngineCell::ExitPrice => Cell::Num(position.exit_price),
        EngineCell::BarsSinceExit => Cell::Int(position.bars_since_exit as i64),
        EngineCell::OpenPositionProfit => Cell::Num(position.open_position_profit),
        EngineCell::PositionProfit => Cell::Num(position.position_profit),
        EngineCell::NetProfit => Cell::Num(position.net_profit),
        EngineCell::GrossProfit => Cell::Num(position.gross_profit),
        EngineCell::GrossLoss => Cell::Num(position.gross_loss),
        EngineCell::TotalTrades => Cell::Int(position.total_trades as i64),
        EngineCell::MaxContractsHeld => Cell::Int(position.max_contracts_held as i64),
        EngineCell::MaxIdDrawdown => Cell::Num(position.max_id_drawdown),
    }
}

/// Collects a typed row per observed bar. Columns are the sorted union of
/// bar-metadata, engine, variable and input names (Python's first-bar trace
/// keys); every bar fills every column.
pub(crate) struct TraceCollector {
    columns: Vec<String>,
    rows: Vec<Vec<Cell>>,
    /// Where each column's cell comes from, in column order.
    sources: Vec<Source>,
    var_types: Vec<TraceKind>,
    /// Inputs never change during a run: their cells are built once.
    input_cells: Vec<Cell>,
}

#[derive(Clone, Copy)]
enum Source {
    BarNumber,
    Date,
    Time,
    Close,
    Engine(EngineCell),
    Var(usize),
    Input(usize),
}

impl TraceCollector {
    /// Bind to `program`. Refuses a program with a traced name whose Python
    /// type cannot be known before the run (see `trace_types`).
    pub(crate) fn new(
        program: &Program,
        var_types: Vec<TraceType>,
        input_types: Vec<TraceType>,
    ) -> Result<Self, KernelError> {
        if let Some(array) = program.arrays.first() {
            return Err(KernelError::unsupported(format!(
                "trace of array {}: array columns are not in the native trace yet",
                array.name
            )));
        }
        let known = |name: &str, ty: TraceType| {
            ty.map_err(|why| KernelError::unsupported(format!("trace of {name}: {why}")))
        };
        // A block-declared variable is a Python local: no column, so its
        // type is never needed.
        let var_types = program
            .var_names
            .iter()
            .zip(var_types)
            .zip(&program.var_block)
            .map(|((name, ty), block)| {
                if *block {
                    Ok(TraceKind::Float)
                } else {
                    known(name, ty)
                }
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut input_cells = Vec::with_capacity(program.inputs.len());
        for ((name, ty), value) in program
            .input_names
            .iter()
            .zip(input_types)
            .zip(&program.inputs)
        {
            let kind = known(name, ty)?;
            input_cells.push(typed_cell(name, kind, Some(value))?);
        }
        let mut named: BTreeMap<String, Source> = BTreeMap::new();
        named.insert("_barnumber".to_owned(), Source::BarNumber);
        named.insert("_date".to_owned(), Source::Date);
        named.insert("_time".to_owned(), Source::Time);
        named.insert("_close".to_owned(), Source::Close);
        for (name, kind) in ENGINE_COLUMNS {
            named.insert((*name).to_owned(), Source::Engine(kind));
        }
        // Python assigns inputs first and variables after, so a variable
        // shadows an input of the same name.
        for (slot, name) in program.input_names.iter().enumerate() {
            named.insert(name.clone(), Source::Input(slot));
        }
        for (slot, name) in program.var_names.iter().enumerate() {
            if !program.var_block[slot] {
                named.insert(name.clone(), Source::Var(slot));
            }
        }
        let (columns, sources) = named.into_iter().unzip();
        Ok(Self {
            columns,
            rows: Vec::new(),
            sources,
            var_types,
            input_cells,
        })
    }

    pub(crate) fn observe(
        &mut self,
        strategy: &Interpreter<'_>,
        position: PositionView,
        bar: Bar,
        index: usize,
    ) -> Result<(), KernelError> {
        let mut row = Vec::with_capacity(self.sources.len());
        for (source, name) in self.sources.iter().zip(&self.columns) {
            row.push(match *source {
                Source::BarNumber => Cell::Int((index + 1) as i64),
                Source::Date => Cell::Int(bar.date),
                Source::Time => Cell::Int(bar.time),
                Source::Close => Cell::Num(bar.close),
                Source::Engine(kind) => engine_cell(kind, &position),
                Source::Var(slot) => {
                    typed_cell(name, self.var_types[slot], strategy.var_value(slot))?
                }
                Source::Input(slot) => self.input_cells[slot].clone(),
            });
        }
        self.rows.push(row);
        Ok(())
    }

    pub(crate) fn into_trace(self) -> TypedTrace {
        TypedTrace {
            columns: self.columns,
            rows: self.rows,
        }
    }
}

/// The cell Python would hold for `value`, given the name's compile-time
/// Python type. A value that does not fit the type is a kernel/typer
/// disagreement: fail the run rather than print a cell Python would not.
fn typed_cell(name: &str, kind: TraceKind, value: Option<&Value>) -> Result<Cell, KernelError> {
    let cell = match (kind, value) {
        (TraceKind::Float, Some(Value::Num(n))) => Some(Cell::Num(*n)),
        (TraceKind::Int, Some(Value::Num(n))) => {
            // Python ints are unbounded; refuse beyond the exact f64 range.
            (n.fract() == 0.0 && n.abs() <= 9_007_199_254_740_992.0).then_some(Cell::Int(*n as i64))
        }
        (TraceKind::Int, Some(Value::Bool(b))) => Some(Cell::Int(i64::from(*b))),
        (TraceKind::Bool, Some(Value::Bool(b))) => Some(Cell::Bool(*b)),
        (TraceKind::Str, Some(Value::NonNum(boxed))) => match &**boxed {
            NonNumeric::Str(s) => Some(Cell::Str(s.clone())),
            NonNumeric::Series(_) => None,
        },
        _ => None,
    };
    cell.ok_or_else(|| {
        KernelError::unsupported(format!(
            "trace of {name}: kernel value {value:?} does not match its Python type {kind:?}"
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cells_follow_the_python_type() {
        let int = |v: &Value| typed_cell("x", TraceKind::Int, Some(v));
        assert_eq!(int(&Value::Num(5.0)).unwrap(), Cell::Int(5));
        assert_eq!(int(&Value::Num(-3.0)).unwrap(), Cell::Int(-3));
        assert_eq!(int(&Value::Bool(true)).unwrap(), Cell::Int(1));
        assert!(int(&Value::Num(2.5)).is_err());
        assert!(int(&Value::Num(1e300)).is_err());
        let float = typed_cell("x", TraceKind::Float, Some(&Value::Num(5.0)));
        assert_eq!(float.unwrap(), Cell::Num(5.0));
        let flag = typed_cell("x", TraceKind::Bool, Some(&Value::Bool(false)));
        assert_eq!(flag.unwrap(), Cell::Bool(false));
        let text = typed_cell("x", TraceKind::Str, Some(&Value::str("LE")));
        assert_eq!(text.unwrap(), Cell::Str("LE".to_owned()));
        assert!(typed_cell("x", TraceKind::Str, Some(&Value::Num(1.0))).is_err());
        assert!(typed_cell("x", TraceKind::Float, None).is_err());
        let series = Value::series(vec![1.0]);
        assert!(typed_cell("x", TraceKind::Str, Some(&series)).is_err());
    }

    #[test]
    fn engine_cells_keep_integer_and_float_distinction() {
        let position = PositionView {
            market_position: 1,
            contracts: 2,
            entry_price: 100.5,
            avg_entry_price: 100.25,
            bars_since_entry: 3,
            exit_price: 0.0,
            bars_since_exit: 0,
            open_position_profit: 10.0,
            position_profit: 10.0,
            net_profit: 10.0,
            gross_profit: 20.0,
            gross_loss: 0.0,
            total_trades: 4,
            max_contracts_held: 2,
            max_id_drawdown: 0.0,
        };
        assert_eq!(
            engine_cell(EngineCell::MarketPosition, &position),
            Cell::Int(1)
        );
        assert_eq!(engine_cell(EngineCell::Contracts, &position), Cell::Int(2));
        assert_eq!(
            engine_cell(EngineCell::TotalTrades, &position),
            Cell::Int(4)
        );
        assert_eq!(
            engine_cell(EngineCell::EntryPrice, &position),
            Cell::Num(100.5)
        );
        assert_eq!(
            engine_cell(EngineCell::NetProfit, &position),
            Cell::Num(10.0)
        );
    }
}
