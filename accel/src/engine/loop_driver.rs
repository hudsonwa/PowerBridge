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

use std::collections::BTreeMap;

use crate::types::{
    Bar, BarTape, Config, Instrument, KernelError, KernelOutcome, NativeStrategy, PositionView,
    SessionEngine, TraceReport,
};

use super::fill::FillEngine;
use super::report::format_trace_row;

type TraceSink<'a> = &'a mut dyn FnMut(&[String]);

/// Run one native strategy over the tape.
///
/// Per bar the order is session-rollover handling, `before_bar`, strategy
/// evaluation, and `after_bar`. This is the same ordering as the Python loop;
/// queued this-bar-on-close orders run before an exit-on-close flatten at a
/// date transition.
pub(crate) fn run_native<S: NativeStrategy>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
) -> Result<KernelOutcome, KernelError> {
    run_native_window(tape, instrument, strategy, 0)
}

/// Run a seeded window. Prefix bars still execute the strategy; fills begin at
/// `trade_from`, and the immediately preceding bar may queue next-bar orders.
pub(crate) fn run_native_window<S: NativeStrategy>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    trade_from: usize,
) -> Result<KernelOutcome, KernelError> {
    run_loop(
        tape,
        instrument,
        None,
        strategy,
        trade_from,
        trade_from,
        |_, _, _, _| Ok(()),
    )
}

/// Configured counterpart to [`run_native`]; preserves the complete explicit
/// config for instrument-dictionary report columns.
pub(crate) fn run_configured<S: NativeStrategy>(
    tape: &BarTape,
    config: &Config,
    strategy: &mut S,
) -> Result<KernelOutcome, KernelError> {
    run_configured_window(tape, config, strategy, 0)
}

/// Configured counterpart to [`run_native_window`].
pub(crate) fn run_configured_window<S: NativeStrategy>(
    tape: &BarTape,
    config: &Config,
    strategy: &mut S,
    trade_from: usize,
) -> Result<KernelOutcome, KernelError> {
    run_loop(
        tape,
        config.into(),
        Some(config),
        strategy,
        trade_from,
        trade_from,
        |_, _, _, _| Ok(()),
    )
}

/// Run natively and build the frozen [`TraceReport`] surface.
///
/// The projection returns strategy-owned trace values as already rendered
/// strings. `None` is the Python missing/`None` cell and renders as `"0"`.
/// Engine/position/statistics columns are then injected with the same names as
/// the Python fill engine. The first bar's sorted keys freeze the columns;
/// later extra keys are ignored and later missing keys render as `"0"`.
/// `emit_gta6` matches Python's `_emit_gta6` switch; general `pl_run` callers
/// pass `false`, while a capture requesting those fields passes `true`.
///
/// No parallel trace type is introduced: the retained result is the frozen
/// `TraceReport` itself.
pub(crate) fn run_native_with_trace<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    emit_gta6: bool,
    project_trace: F,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_native_window_with_trace(tape, instrument, strategy, 0, 0, emit_gta6, project_trace)
}

/// Configured counterpart to [`run_native_with_trace`].
pub(crate) fn run_configured_with_trace<S, F>(
    tape: &BarTape,
    config: &Config,
    strategy: &mut S,
    emit_gta6: bool,
    project_trace: F,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_configured_window_with_trace(tape, config, strategy, 0, 0, emit_gta6, project_trace)
}

/// Seeded-window variant of [`run_native_with_trace`].
///
/// `trade_from` suppresses prefix fills while preserving strategy state;
/// `warmup` independently suppresses report rows. Both are zero-based tape
/// indices and must not exceed the tape length.
pub(crate) fn run_native_window_with_trace<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    trade_from: usize,
    warmup: usize,
    emit_gta6: bool,
    project_trace: F,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_native_window_with_trace_sink(
        tape,
        instrument,
        strategy,
        trade_from,
        warmup,
        emit_gta6,
        project_trace,
        None,
        None,
        None,
    )
}

/// Configured seeded-window trace run.
pub(crate) fn run_configured_window_with_trace<S, F>(
    tape: &BarTape,
    config: &Config,
    strategy: &mut S,
    trade_from: usize,
    warmup: usize,
    emit_gta6: bool,
    project_trace: F,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_configured_window_with_trace_sink(
        tape,
        config,
        strategy,
        trade_from,
        warmup,
        emit_gta6,
        project_trace,
        None,
        None,
        None,
    )
}

/// Whole-tape trace run with an optional row/header sink.
///
/// `retain_rows` mirrors `run_el`: `None` retains when no `row_sink` is given
/// and drops retained rows when a sink is given. Pass `Some(true)` to sink
/// and retain.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_native_with_trace_sink<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    emit_gta6: bool,
    project_trace: F,
    row_sink: Option<TraceSink<'_>>,
    header_sink: Option<TraceSink<'_>>,
    retain_rows: Option<bool>,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_native_window_with_trace_sink(
        tape,
        instrument,
        strategy,
        0,
        0,
        emit_gta6,
        project_trace,
        row_sink,
        header_sink,
        retain_rows,
    )
}

/// Configured counterpart to [`run_native_with_trace_sink`].
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_configured_with_trace_sink<S, F>(
    tape: &BarTape,
    config: &Config,
    strategy: &mut S,
    emit_gta6: bool,
    project_trace: F,
    row_sink: Option<TraceSink<'_>>,
    header_sink: Option<TraceSink<'_>>,
    retain_rows: Option<bool>,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_configured_window_with_trace_sink(
        tape,
        config,
        strategy,
        0,
        0,
        emit_gta6,
        project_trace,
        row_sink,
        header_sink,
        retain_rows,
    )
}

/// Seeded-window trace run with an optional row/header sink.
///
/// Prefix/warmup suppression is unchanged: a row is emitted only when
/// `index >= trade_from && index >= warmup`. `retain_rows` mirrors `run_el`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_native_window_with_trace_sink<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    strategy: &mut S,
    trade_from: usize,
    warmup: usize,
    emit_gta6: bool,
    project_trace: F,
    row_sink: Option<TraceSink<'_>>,
    header_sink: Option<TraceSink<'_>>,
    retain_rows: Option<bool>,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_window_with_trace(
        tape,
        instrument,
        None,
        strategy,
        trade_from,
        warmup,
        emit_gta6,
        project_trace,
        row_sink,
        header_sink,
        retain_rows,
    )
}

/// Configured seeded-window trace run with an optional row/header sink.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_configured_window_with_trace_sink<S, F>(
    tape: &BarTape,
    config: &Config,
    strategy: &mut S,
    trade_from: usize,
    warmup: usize,
    emit_gta6: bool,
    project_trace: F,
    row_sink: Option<TraceSink<'_>>,
    header_sink: Option<TraceSink<'_>>,
    retain_rows: Option<bool>,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    run_window_with_trace(
        tape,
        config.into(),
        Some(config),
        strategy,
        trade_from,
        warmup,
        emit_gta6,
        project_trace,
        row_sink,
        header_sink,
        retain_rows,
    )
}

#[allow(clippy::too_many_arguments)]
fn run_window_with_trace<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    config: Option<&Config>,
    strategy: &mut S,
    trade_from: usize,
    warmup: usize,
    emit_gta6: bool,
    mut project_trace: F,
    mut row_sink: Option<TraceSink<'_>>,
    mut header_sink: Option<TraceSink<'_>>,
    retain_rows: Option<bool>,
) -> Result<(KernelOutcome, TraceReport), KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, PositionView) -> Result<BTreeMap<String, Option<String>>, KernelError>,
{
    let retain = retain_rows.unwrap_or(row_sink.is_none());
    let mut report = TraceReport {
        columns: Vec::new(),
        rows: Vec::new(),
    };
    let mut header_sent = false;
    let outcome = run_loop(
        tape,
        instrument,
        config,
        strategy,
        trade_from,
        warmup,
        |strategy, bar, bar_number, engine| {
            let values = project_trace(strategy, bar, bar_number, engine.position())?;
            let row = format_trace_row(&mut report, values, engine, emit_gta6);
            if !header_sent {
                if let Some(sink) = header_sink.as_mut() {
                    sink(&report.columns);
                }
                header_sent = true;
            }
            if let Some(sink) = row_sink.as_mut() {
                sink(&row);
            }
            if retain {
                report.rows.push(row);
            }
            Ok(())
        },
    )?;
    if !header_sent {
        if let Some(sink) = header_sink.as_mut() {
            sink(&report.columns);
        }
    }
    Ok((outcome, report))
}

fn run_loop<S, F>(
    tape: &BarTape,
    instrument: Instrument,
    config: Option<&Config>,
    strategy: &mut S,
    trade_from: usize,
    warmup: usize,
    mut after_bar: F,
) -> Result<KernelOutcome, KernelError>
where
    S: NativeStrategy,
    F: FnMut(&S, Bar, usize, &FillEngine) -> Result<(), KernelError>,
{
    if trade_from > tape.len() {
        return Err(KernelError::input(format!(
            "trade_from {trade_from} exceeds tape length {}",
            tape.len()
        )));
    }
    if warmup > tape.len() {
        return Err(KernelError::input(format!(
            "warmup {warmup} exceeds tape length {}",
            tape.len()
        )));
    }

    let mut engine = config.map_or_else(|| FillEngine::new(instrument), FillEngine::from_config);
    let mut previous_date = None;
    let mut previous_close = 0.0;
    let mut previous_time = 0;

    for index in 0..tape.len() {
        let bar_number = index
            .checked_add(1)
            .ok_or_else(|| KernelError::input("bar number overflow"))?;
        let bar = tape.bar(index);
        let trading = index >= trade_from;
        let queue_orders = trade_from == 0 || index + 1 >= trade_from;

        if trading && previous_date.is_some_and(|date| date != bar.date) && engine.exit_on_close() {
            engine.execute_on_close_pending()?;
            if engine.position().market_position != 0 {
                engine.close_for_session(
                    previous_close,
                    bar_number - 1,
                    previous_date.unwrap_or(0),
                    previous_time,
                    "End of Day Exit",
                    4,
                )?;
            }
        }

        if trading {
            engine.before_bar(bar, bar_number)?;
        }
        let output = strategy
            .on_bar(bar, bar_number, engine.position())
            .map_err(|error| KernelError::strategy(format!("bar {bar_number}: {error}")))?;
        if queue_orders {
            engine.after_bar(output, bar, bar_number)?;
        } else {
            engine.apply_risk(output.risk);
        }
        if index >= trade_from && index >= warmup {
            after_bar(strategy, bar, bar_number, &engine)?;
        }

        previous_date = Some(bar.date);
        previous_close = bar.close;
        previous_time = bar.time;
    }

    let position = engine.position();
    let total_trades = engine.total_trades();
    let max_id_drawdown = engine.max_id_drawdown();
    let fast_path_hits = engine.fast_path_hits();
    let (fills, closed_positions) = engine.into_ledgers();
    Ok(KernelOutcome {
        fills,
        position,
        total_trades,
        max_id_drawdown,
        closed_positions,
        skipped_bars: 0,
        fast_path_hits,
    })
}
