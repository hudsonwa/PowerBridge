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

//! Typed per-bar trace — the `run_program_trace` sink contract (issue #42).
//!
//! The trace sink emits **typed** cells (not the pre-formatted `String` cells of
//! [`crate::engine::report::format_trace_row`]): Rust returns the raw `f64` /
//! `i64` / `bool` / `String` values in the kernel's registers, and Python owns
//! text rendering (`str(value)`). This is the decision the reduced-trace gate
//! (issue #36, "do not bridge the existing sink as-is") directs: the 24-column
//! text CSV is the *consumer's* formatting problem, not the kernel's.
//!
//! The row set is the **full** per-bar trace (one row per evaluated bar), not
//! the transition-only `audit-event-v1` packet — that packet remains the
//! measured sub-second path; this bridge is the general, reference surface that
//! reproduces the Python `run_el` trace shape but typed.

use std::collections::{BTreeMap, BTreeSet};

use crate::types::{Bar, PositionView};

use super::interp::Interpreter;
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

/// The frozen typed trace surface: `columns` are the sorted column names
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

/// Collects a typed row per observed bar. Columns freeze from the first bar's
/// sorted union of bar-metadata, engine and strategy keys; later bars align to
/// that fixed order (missing renders as `Int(0)`).
pub(crate) struct TraceCollector {
    columns: Vec<String>,
    rows: Vec<Vec<Cell>>,
}

impl TraceCollector {
    pub(crate) fn new() -> Self {
        Self {
            columns: Vec::new(),
            rows: Vec::new(),
        }
    }

    fn ensure_columns(&mut self, strategy: &Interpreter<'_>) {
        if !self.columns.is_empty() {
            return;
        }
        let mut names: BTreeSet<String> = BTreeSet::new();
        names.insert("_barnumber".to_owned());
        names.insert("_date".to_owned());
        names.insert("_time".to_owned());
        names.insert("_close".to_owned());
        for (name, _) in ENGINE_COLUMNS {
            names.insert((*name).to_owned());
        }
        for name in strategy.var_names() {
            names.insert(name.clone());
        }
        self.columns = names.into_iter().collect();
    }

    pub(crate) fn observe(
        &mut self,
        strategy: &Interpreter<'_>,
        position: PositionView,
        bar: Bar,
        index: usize,
    ) {
        self.ensure_columns(strategy);
        let mut cells: BTreeMap<String, Cell> = BTreeMap::new();
        for slot in 0..strategy.var_names().len() {
            let cell = value_to_cell(strategy.var_value(slot));
            cells.insert(strategy.var_names()[slot].clone(), cell);
        }
        cells.insert("_barnumber".to_owned(), Cell::Int((index + 1) as i64));
        cells.insert("_date".to_owned(), Cell::Int(bar.date));
        cells.insert("_time".to_owned(), Cell::Int(bar.time));
        cells.insert("_close".to_owned(), Cell::Num(bar.close));
        for (name, kind) in ENGINE_COLUMNS {
            cells.insert((*name).to_owned(), engine_cell(kind, &position));
        }
        let row = self
            .columns
            .iter()
            .map(|name| cells.remove(name).unwrap_or(Cell::Int(0)))
            .collect();
        self.rows.push(row);
    }

    pub(crate) fn into_trace(self) -> TypedTrace {
        TypedTrace {
            columns: self.columns,
            rows: self.rows,
        }
    }
}

/// Project one strategy variable into a typed cell. Unset or series-valued
/// slots render as `Num(0.0)` (the typed counterpart of Python's missing-`'0'`),
/// matching `report.rs`'s "missing renders as 0" rule.
fn value_to_cell(value: Option<&Value>) -> Cell {
    match value {
        Some(Value::Num(n)) => Cell::Num(*n),
        Some(Value::Bool(b)) => Cell::Bool(*b),
        Some(Value::NonNum(boxed)) => match &**boxed {
            NonNumeric::Str(s) => Cell::Str(s.clone()),
            NonNumeric::Series(_) => Cell::Num(0.0),
        },
        None => Cell::Num(0.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_and_series_values_render_as_zero() {
        assert_eq!(value_to_cell(None), Cell::Num(0.0));
        assert_eq!(value_to_cell(Some(&Value::Num(3.5))), Cell::Num(3.5));
        assert_eq!(value_to_cell(Some(&Value::Bool(true))), Cell::Bool(true));
        assert_eq!(
            value_to_cell(Some(&Value::str("LE"))),
            Cell::Str("LE".to_owned())
        );
        assert_eq!(
            value_to_cell(Some(&Value::series(vec![1.0]))),
            Cell::Num(0.0)
        );
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
