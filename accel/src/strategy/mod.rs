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

//! IR interpreter: execute a v2 `program_ir` bar-by-bar and emit fills.
//!
//! This module owns the strategy/interpreter seam ([`NativeStrategy`]): it
//! reads the program from IR, takes Inputs as parameters, and drives the
//! existing [`crate::kernel::run_native`] bar/fill engine. Anything the
//! dialect supports that this interpreter does not implement is refused
//! loudly — never approximated.

use std::collections::HashMap;
use std::sync::Arc;

use crate::audit::{self, AuditCollector, NQ_ROUND_TURN_COST};
use crate::kernel::{run_native, run_native_from_observing, run_native_from_offset};
use crate::types::{
    BarTape, Config, Fill, Instrument, KernelError, KernelOutcome, NativeStrategy, SkipPlan,
};

#[cfg(test)]
mod bench;
mod interp;
mod ir;
#[cfg(test)]
mod parity_tests;
mod trace;
mod value;

mod compile;
mod drawing;
mod skip;
mod specialize;
use compile::{compile, resolve_inputs};
use interp::{align_data2, Data2Aligned, Interpreter};
use ir::{ast_uses_data2, load_program_ir};
pub(crate) use trace::{Cell, TypedTrace};
pub(crate) use value::{canonical_fill_line, Value};

/// Execute `program_ir` over `series` with explicit `config`.
///
/// `data2` must be present iff the program references Data2 (`Close of Data2`);
/// it is aligned to `series` by `(date, time)`, with Data1 bars that have no
/// matching Data2 bar falling back to the primary series value — the same
/// `kwargs.get('data2_<series>', <series>[-1])` fallback the Python codegen
/// emits. `Data3`/higher and non-bar Data2 series (`ticks`, …) are refused
/// loudly. `inputs` overrides IR Input defaults (empty map = use the defaults
/// in the program).
///
/// The FFI `run_program` entry point is the intended caller; unit tests drive it
/// directly. `#[allow(dead_code)]` is for the cdylib build, which does not
/// yet export this function.
#[allow(dead_code)]
pub(crate) fn run_program(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    inputs: &HashMap<String, Value>,
) -> Result<KernelOutcome, KernelError> {
    run_program_inputs(program_ir, series, data2, config, inputs)
}

/// Execute `program_ir` with explicit `inputs` (the [`run_program`] body).
pub(crate) fn run_program_inputs(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    inputs: &HashMap<String, Value>,
) -> Result<KernelOutcome, KernelError> {
    let ast = load_program_ir(program_ir)?;
    let data2 = prepare_data2(&ast, series, data2)?;
    let mut strategy = Interpreter::new(ast, inputs, data2)?;
    strategy.set_drawing_tick(config.tick);
    run_with_max_bars_back(series, config, &mut strategy)
}

/// Like [`run_program`], and the drawing objects the run recorded.
///
/// The JSON array is the object list of a `powerbridge-drawing-dump` v1
/// (`seq` is 0; the Python dump builder assigns creation order). `producer`
/// is not included. Colour and style defaults are the measured MultiCharts
/// values. A drawing call this kernel does not implement is still refused.
pub(crate) fn run_program_drawings(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    inputs: &HashMap<String, Value>,
) -> Result<(Vec<Fill>, String), KernelError> {
    let ast = load_program_ir(program_ir)?;
    let data2 = prepare_data2(&ast, series, data2)?;
    let mut strategy = Interpreter::new(ast, inputs, data2)?;
    strategy.set_drawing_tick(config.tick);
    let outcome = run_with_max_bars_back(series, config, &mut strategy)?;
    Ok((outcome.fills, strategy.drawings_json().to_string()))
}

/// Run `strategy` honouring `config.max_bars_back`.
///
/// 0 is the unchanged [`run_native`] path. N > 0 feeds the first N bars as
/// history only (no evaluation, no orders), then evaluates the rest with
/// `BarNumber` starting at 1 — the MultiCharts MaxBarsBack convention. The
/// skip-region proof is not used on that path: every evaluated bar runs.
fn run_with_max_bars_back(
    series: &BarTape,
    config: &Config,
    strategy: &mut Interpreter<'_>,
) -> Result<KernelOutcome, KernelError> {
    if config.max_bars_back == 0 {
        return run_native(series, config.into(), strategy);
    }
    let skip = config.max_bars_back.min(series.len());
    for index in 0..skip {
        strategy.observe_history(series.bar(index));
    }
    let rest = BarTape::from_columns(
        series.opens()[skip..].to_vec(),
        series.highs()[skip..].to_vec(),
        series.lows()[skip..].to_vec(),
        series.closes()[skip..].to_vec(),
        series.volumes()[skip..].to_vec(),
        series.dates()[skip..].to_vec(),
        series.times()[skip..].to_vec(),
    )?;
    run_native_from_offset(&rest, config.into(), strategy, skip, |_, _, _, _| Ok(()))
}

/// The trace, audit and batch entry points do not implement MaxBarsBack or the
/// same-direction entry cap yet; refuse rather than silently ignore them.
fn refuse_run_program_options(config: &Config, entry: &str) -> Result<(), KernelError> {
    if config.max_bars_back != 0 {
        return Err(KernelError::unsupported(format!(
            "max_bars_back > 0 is not supported by {entry}"
        )));
    }
    // Likewise the same-direction entry cap: only `run_program` honours it.
    if config.max_entries_per_direction != 0 {
        return Err(KernelError::unsupported(format!(
            "max_entries_per_direction is not supported by {entry}"
        )));
    }
    Ok(())
}

/// Produce the typed per-bar trace plus the outcome fills.
pub(crate) fn run_program_trace(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
) -> Result<(TypedTrace, Vec<Fill>), KernelError> {
    refuse_run_program_options(config, "run_program_trace")?;
    let ast = load_program_ir(program_ir)?;
    let data2_aligned = prepare_data2(&ast, series, data2)?;
    let mut strategy = Interpreter::new(ast, &HashMap::new(), data2_aligned)?;
    let instrument: Instrument = config.into();
    let mut collector = trace::TraceCollector::new();
    let outcome = run_native_from_offset(
        series,
        instrument,
        &mut strategy,
        0,
        |s, position, bar, index| {
            collector.observe(s, position, bar, index);
            Ok(())
        },
    )?;
    Ok((collector.into_trace(), outcome.fills))
}

/// Production [`run_native`] loop plus a typed `audit-event-v1` packet.
///
/// Same skip-plan and fill engine as [`run_program`]. The packet is one framed
/// `bytes` blob (header + transition records + trade records). Fills in the
/// outcome must match [`run_program`] bit-for-bit; that is the first gate.
pub(crate) fn run_program_audit_packet(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    inputs: &HashMap<String, Value>,
    hash_tapes: bool,
) -> Result<(KernelOutcome, Vec<u8>), KernelError> {
    refuse_run_program_options(config, "run_program_audit_packet")?;
    let ast = load_program_ir(program_ir)?;
    let data2_aligned = prepare_data2(&ast, series, data2)?;
    let mut strategy = Interpreter::new(ast, inputs, data2_aligned)?;
    let instrument: Instrument = config.into();
    let mut collector = AuditCollector::bind(
        &strategy,
        instrument.tick,
        instrument.point_value,
        NQ_ROUND_TURN_COST,
    )?;
    let plan = strategy.skip_plan(series);
    if !crate::kernel::diagnostics_enabled() {
        // [accel] diagnostics are opt-in (--verbose / PB_DEBUG).
    } else if !plan.proven {
        eprintln!("[accel] skip-region: full evaluation — {}", plan.reason);
    } else if plan.eval_from > 0 {
        eprintln!(
            "[accel] skip-region: skipped {} of {} bars ({})",
            plan.eval_from,
            series.len(),
            plan.reason
        );
    }
    let outcome = run_native_from_observing(
        series,
        instrument,
        &mut strategy,
        plan.eval_from,
        |strategy, position, bar, index| collector.observe(strategy, position, bar, index),
    )?;
    let fill_digest = audit::fill_digest_from_lines(outcome.fills.iter().map(canonical_fill_line));
    let packet = collector.finish(
        &outcome,
        series,
        program_ir,
        config,
        fill_digest,
        hash_tapes,
        series.len(),
    );
    Ok((outcome, packet))
}

/// Prove a prefix skip from the IR and the Data1 date column alone (no OHLC).
///
/// The skip plan only ever reads tape dates + the program, so a slice-before-
/// marshal caller can ask for `eval_from` without first marshalling the seven
/// OHLC columns across the FFI. OHLC is never read by the skip proof.
pub(crate) fn skip_eval_from(
    program_ir: &str,
    dates: &[i64],
    inputs: &HashMap<String, Value>,
) -> Result<SkipPlan, KernelError> {
    let ast = load_program_ir(program_ir)?;
    let base = compile(&ast, &HashMap::new())?;
    let mut program = base;
    program.inputs = resolve_inputs(&ast, inputs)?;
    Ok(skip::plan(&program, dates))
}

/// One `audit-event-v1` packet from an already-sliced `series` (`full[eval_from..]`).
///
/// Matches [`run_program_audit_packet`] byte-for-byte for the same `inputs` and
/// `config` when `eval_from` is that program's proven prefix skip and
/// `source_bar_count` is the full tape length — the slice-before-marshal solo
/// reference.
pub(crate) fn run_program_audit_packet_sliced(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    inputs: &HashMap<String, Value>,
    eval_from: usize,
    source_bar_count: usize,
) -> Result<Vec<u8>, KernelError> {
    refuse_run_program_options(config, "run_program_audit_packet_sliced")?;
    let ast = load_program_ir(program_ir)?;
    let data2_aligned = prepare_data2(&ast, series, data2)?;
    let mut strategy = Interpreter::new(ast, inputs, data2_aligned)?;
    let instrument: Instrument = config.into();
    let mut collector = AuditCollector::bind(
        &strategy,
        instrument.tick,
        instrument.point_value,
        NQ_ROUND_TURN_COST,
    )?;
    let outcome = run_native_from_offset(
        series,
        instrument,
        &mut strategy,
        eval_from,
        |strategy, position, bar, index| collector.observe(strategy, position, bar, index),
    )?;
    let fill_digest = audit::fill_digest_from_lines(outcome.fills.iter().map(canonical_fill_line));
    let packet = collector.finish(
        &outcome,
        series,
        program_ir,
        config,
        fill_digest,
        false,
        source_bar_count,
    );
    Ok(packet)
}

/// `K` `audit-event-v1` packets over ONE already-sliced `series`, compiled once.
///
/// Each variant reuses the shared compiled program (only its resolved input
/// vector differs, exactly like [`run_program_batch`]) and the slice/marshal
/// already paid for once at the Python boundary. `out[i]` is byte-identical to
/// [`run_program_audit_packet_sliced`] with `variants[i]` — the sweep invariant.
///
/// `eval_from` is the absolute index the slice starts at and `source_bar_count`
/// the full tape length; both feed the packet header so a sliced packet is
/// byte-identical to the full-tape packet (see
/// [`run_program_audit_packet_sliced`]).
pub(crate) fn run_program_audit_packet_batch(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    variants: &[HashMap<String, Value>],
    eval_from: usize,
    source_bar_count: usize,
) -> Result<Vec<Vec<u8>>, KernelError> {
    refuse_run_program_options(config, "run_program_audit_packet_batch")?;
    let ast = load_program_ir(program_ir)?;
    let data2 = prepare_data2(&ast, series, data2)?;
    let base = compile(&ast, &HashMap::new())?;
    let instrument: Instrument = config.into();
    let mut out = Vec::with_capacity(variants.len());
    for overrides in variants {
        let mut program = base.clone();
        program.inputs = resolve_inputs(&ast, overrides)?;
        let mut strategy = Interpreter::from_program(program, data2.clone())?;
        let mut collector = AuditCollector::bind(
            &strategy,
            instrument.tick,
            instrument.point_value,
            NQ_ROUND_TURN_COST,
        )?;
        let outcome = run_native_from_offset(
            series,
            instrument,
            &mut strategy,
            eval_from,
            |strategy, position, bar, index| collector.observe(strategy, position, bar, index),
        )?;
        let fill_digest =
            audit::fill_digest_from_lines(outcome.fills.iter().map(canonical_fill_line));
        let packet = collector.finish(
            &outcome,
            series,
            program_ir,
            config,
            fill_digest,
            false,
            source_bar_count,
        );
        out.push(packet);
    }
    Ok(out)
}

/// Run a sweep: `K` input variants over ONE tape and ONE already-loaded program.
///
/// Compile happens once (the instruction stream and per-variable metadata are
/// identical across variants — only the resolved input vector differs) and each
/// variant reuses that compiled program, so a sweep of `K` variants pays the
/// JSON parse / AST lower / tape marshal cost a single time, not `K` times.
///
/// Returns the ordered per-variant fills, `out[i] == run_program(...) fills`
/// with the `i`-th variant's inputs — bit-for-bit. That invariant
/// (`solo_digest == batch_digest`) is the correctness gate for the sweep/batch path.
pub(crate) fn run_program_batch(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    variants: &[HashMap<String, Value>],
) -> Result<Vec<Vec<Fill>>, KernelError> {
    refuse_run_program_options(config, "run_program_batch")?;
    let ast = load_program_ir(program_ir)?;
    let data2 = prepare_data2(&ast, series, data2)?;
    let base = compile(&ast, &HashMap::new())?;
    let instrument = config.into();
    let mut out = Vec::with_capacity(variants.len());
    for overrides in variants {
        let mut program = base.clone();
        program.inputs = resolve_inputs(&ast, overrides)?;
        let mut interp = Interpreter::from_program(program, data2.clone())?;
        let outcome = run_native(series, instrument, &mut interp)?;
        out.push(outcome.fills);
    }
    Ok(out)
}

/// Align (or refuse) the Data2 tape for a program, wrapped in an `Arc` so a
/// sweep shares the one aligned Data2 across every variant (and thread) without
/// a per-variant clone of ~8×N columns.
fn prepare_data2<'a>(
    ast: &serde_json::Value,
    series: &BarTape,
    data2: Option<&'a BarTape>,
) -> Result<Option<Arc<Data2Aligned<'a>>>, KernelError> {
    match (ast_uses_data2(ast), data2) {
        (true, Some(tape)) => Ok(Some(Arc::new(align_data2(series, tape)))),
        (true, None) => Err(KernelError::unsupported(
            "program references Data2 but no data2 series was supplied",
        )),
        (false, Some(_)) => Err(KernelError::unsupported(
            "data2 supplied but program does not reference Data2",
        )),
        (false, None) => Ok(None),
    }
}

/// Fan a sweep across `threads` worker threads over the one shared tape and the
/// one shared compiled program. Each worker claims variant indices from a shared
/// counter, clones the (cheap) compiled program, swaps in its resolved inputs,
/// and runs the bar loop natively. Results are materialised in variant order and
/// are bit-identical to the sequential [`run_program_batch`] — parallelism must
/// never change an answer (a thread only ever reads the shared tape/Data2).
pub(crate) fn run_program_batch_parallel(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    variants: &[HashMap<String, Value>],
    threads: usize,
) -> Result<Vec<Vec<Fill>>, KernelError> {
    refuse_run_program_options(config, "run_program_batch_parallel")?;
    let ast = load_program_ir(program_ir)?;
    let data2 = prepare_data2(&ast, series, data2)?;
    let base = compile(&ast, &HashMap::new())?;
    let instrument = config.into();
    let inputs: Vec<Vec<Value>> = variants
        .iter()
        .map(|o| resolve_inputs(&ast, o))
        .collect::<Result<_, _>>()?;

    let n = variants.len();
    let nthreads = threads.clamp(1, n.max(1));
    let next = std::sync::atomic::AtomicUsize::new(0);
    let slots = std::sync::Mutex::new(vec![None; n]);

    std::thread::scope(|scope| {
        for _ in 0..nthreads {
            scope.spawn(|| loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                if i >= n {
                    break;
                }
                let mut program = base.clone();
                program.inputs = inputs[i].clone();
                let result =
                    Interpreter::from_program(program, data2.clone()).and_then(|mut interp| {
                        run_native(series, instrument, &mut interp).map(|o| o.fills)
                    });
                slots.lock().unwrap()[i] = Some(result);
            });
        }
    });

    let mut slots = slots.into_inner().unwrap();
    let mut out = Vec::with_capacity(n);
    for slot in slots.drain(..) {
        out.push(slot.expect("every variant index claimed exactly once")?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::value::{canonical_fill_line, python_float_hex};
    use super::*;
    use crate::types::{BarTape, Config, Fill, Session};
    use std::fs;
    use std::path::PathBuf;

    fn nq_config() -> Config {
        Config {
            name: "NQ".into(),
            bigpointvalue: 20.0,
            pointvalue: 0.20,
            minmove: 25,
            pricescale: 100,
            tick: 0.25,
            barinterval: 1,
            session: Session {
                start: 830,
                end: 1515,
            },
            commission: 0.0,
            slippage: 0.0,
            max_bars_back: 0,
            max_entries_per_direction: 0,
            warmup_seed: 0,
            symccy: 1,
            symnm: "@NQ#C".into(),
            symalt: "@NQ#C".into(),
            descstr: "E-MINI NASDAQ 100".into(),
            expdate: 1130331,
        }
    }

    fn load_csv(path: &str) -> BarTape {
        let text = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let mut open = Vec::new();
        let mut high = Vec::new();
        let mut low = Vec::new();
        let mut close = Vec::new();
        let mut volume = Vec::new();
        let mut date = Vec::new();
        let mut time = Vec::new();
        for (i, line) in text.lines().enumerate() {
            if i == 0 || line.trim().is_empty() {
                continue;
            }
            let cols: Vec<&str> = line.split(',').collect();
            assert!(cols.len() >= 7, "bad csv row {i}: {line}");
            date.push(cols[0].trim().parse().unwrap());
            time.push(cols[1].trim().parse().unwrap());
            open.push(cols[2].trim().parse().unwrap());
            high.push(cols[3].trim().parse().unwrap());
            low.push(cols[4].trim().parse().unwrap());
            close.push(cols[5].trim().parse().unwrap());
            volume.push(cols[6].trim().parse().unwrap());
        }
        BarTape::from_columns(open, high, low, close, volume, date, time).unwrap()
    }

    fn sample_bars_path() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../examples/NQ_sample_bars.csv")
    }

    fn dump_fills(fills: &[Fill]) {
        eprintln!("native fills: {}", fills.len());
        for (i, fill) in fills.iter().enumerate() {
            eprintln!("  {:>2} {}", i + 1, canonical_fill_line(fill));
        }
    }

    #[test]
    fn python_float_hex_matches_known_gt2_prices() {
        assert_eq!(python_float_hex(15028.5), "0x1.d5a4000000000p+13");
        assert_eq!(python_float_hex(15017.75), "0x1.d54e000000000p+13");
        assert_eq!(python_float_hex(0.0), "0x0.0p+0");
        assert_eq!(python_float_hex(1.0), "0x1.0000000000000p+0");
    }

    #[test]
    fn gt2_sample_emits_python_fills() {
        let ir = include_str!("fixtures/gt2.ir.json");
        let tape = load_csv(sample_bars_path().to_str().unwrap());
        assert_eq!(tape.len(), 140, "NQ_sample_bars.csv bar count");
        let outcome = run_program(ir, &tape, None, &nq_config(), &HashMap::new())
            .unwrap_or_else(|e| panic!("native run_program failed: {e}"));
        dump_fills(&outcome.fills);

        let expected: &[(&str, &str, i64, i64, f64)] = &[
            ("ExitLong", "SE", 1240102, 1001, 15028.5),
            ("EntryLong", "trailing_stop", 1240102, 1026, 14959.25),
            ("EntryLong", "LE", 1240102, 1034, 14967.14),
            ("ExitLong", "profit_target", 1240102, 1051, 15067.25),
            ("ExitLong", "SE", 1240102, 1109, 15017.75),
            ("EntryLong", "trailing_stop", 1240102, 1136, 14950.0),
            ("EntryLong", "LE", 1240102, 1144, 14985.45),
        ];
        assert_eq!(
            outcome.fills.len(),
            expected.len(),
            "fill count: native={} python={}",
            outcome.fills.len(),
            expected.len()
        );
        for (i, (fill, (ty, sig, date, time, px))) in
            outcome.fills.iter().zip(expected.iter()).enumerate()
        {
            assert_eq!(fill.fill_type, *ty, "fill {i} type");
            assert_eq!(fill.signal, *sig, "fill {i} signal");
            assert_eq!(fill.date, *date, "fill {i} date");
            assert_eq!(fill.time, *time, "fill {i} time");
            assert_eq!(fill.contracts, 1, "fill {i} contracts");
            assert_eq!(
                python_float_hex(fill.price),
                python_float_hex(*px),
                "fill {i} price native={} python={}",
                fill.price,
                px
            );
        }
    }

    #[test]
    fn max_bars_back_suppresses_warm_up_bars() {
        let ir = include_str!("fixtures/gt2.ir.json");
        let tape = load_csv(sample_bars_path().to_str().unwrap());
        let full = run_program(ir, &tape, None, &nq_config(), &HashMap::new()).unwrap();
        let mut config = nq_config();
        config.max_bars_back = 50;
        let skipped = run_program(ir, &tape, None, &config, &HashMap::new()).unwrap();
        let first_eval = tape.bar(50);
        // No fill may land before the first evaluated bar.
        for fill in &skipped.fills {
            assert!(
                (fill.date, fill.time) >= (first_eval.date, first_eval.time),
                "fill before bar 51: {}",
                canonical_fill_line(fill)
            );
        }
        assert_ne!(full.fills.len(), skipped.fills.len());
        // 0 is the unchanged path; MaxBarsBack past the tape evaluates nothing.
        config.max_bars_back = tape.len() + 5;
        let none = run_program(ir, &tape, None, &config, &HashMap::new()).unwrap();
        assert!(none.fills.is_empty());
        let error = run_program_trace(ir, &tape, None, &config).unwrap_err();
        assert!(error.to_string().contains("max_bars_back"), "{error}");
    }

    #[test]
    fn typed_trace_carries_vars_and_matches_run_program_fills() {
        let ir = include_str!("fixtures/gt2.ir.json");
        let tape = load_csv(sample_bars_path().to_str().unwrap());
        let fills = run_program(ir, &tape, None, &nq_config(), &HashMap::new())
            .unwrap()
            .fills;
        let (trace, trace_fills) = run_program_trace(ir, &tape, None, &nq_config()).unwrap();

        // The trace run's fills are the same ordered trade list, bit-for-bit.
        assert_eq!(trace_fills, fills, "trace run must produce the same fills");
        // One typed row per evaluated bar.
        assert_eq!(trace.rows.len(), 140);
        // Columns are sorted and duplicate-free.
        let mut sorted = trace.columns.clone();
        sorted.sort();
        assert_eq!(trace.columns, sorted);
        let index_of = |name: &str| {
            trace
                .columns
                .iter()
                .position(|column| column == name)
                .unwrap_or_else(|| panic!("missing trace column {name}"))
        };
        // Bar metadata the report reads.
        for name in ["_date", "_time", "_barnumber", "_close"] {
            index_of(name);
        }
        // Engine position columns the report reads.
        for name in [
            "current_contracts",
            "entry_price",
            "net_profit",
            "totaltrades",
        ] {
            index_of(name);
        }
        // Strategy variables are projected.
        for name in ["fastma", "slowma", "golong", "goshort"] {
            index_of(name);
        }
        // The date cell is typed Int (not a str) — the sink is typed.
        let date_idx = index_of("_date");
        assert!(matches!(trace.rows[0][date_idx], super::Cell::Int(_)));
    }

    fn nq_slot_var_decls() -> String {
        let decls: Vec<String> = crate::audit::NQ_AUDIT_SLOT_NAMES
            .iter()
            .map(|name| {
                let init = if matches!(
                    *name,
                    "historyready" | "c1gate" | "entrygate" | "emergencyexit" | "eodexit"
                ) {
                    r#"{"type":"boolean","value":false}"#
                } else {
                    r#"{"type":"number","value":"0"}"#
                };
                format!(r#"{{"data_ref":null,"init":{init},"name":"{name}"}}"#)
            })
            .collect();
        format!(r#"{{"type":"var_decl","decls":[{}]}}"#, decls.join(","))
    }

    fn wrap_ir(body: &str) -> String {
        format!(
            r#"{{"ir_version":1,"kind":"pl_transpiler.ast","ast":{{"type":"program","body":[{body}]}}}}"#
        )
    }

    fn order_if_bar(bar: i64, action: &str, label: &str, qty: &str) -> String {
        format!(
            r#"{{"type":"if","then_block":true,"else_body":null,"cond":{{"type":"compare","op":"=","left":{{"type":"position_kw","kwarg":"barnumber"}},"right":{{"type":"number","value":"{bar}"}}}},"body":[{{"type":"order","action":"{action}","bar_timing":"next","entry_label":null,"label":{{"type":"string","value":"{label}"}},"order_type":"market","price":null,"quantity":{qty}}}]}}"#
        )
    }

    #[test]
    fn audit_packet_refuses_programs_missing_nq_slots() {
        let ir = include_str!("fixtures/gt2.ir.json");
        let tape = load_csv(sample_bars_path().to_str().unwrap());
        let err = run_program_audit_packet(ir, &tape, None, &nq_config(), &HashMap::new(), false)
            .expect_err("GT2 has no NQ audit slots");
        let message = err.to_string();
        assert!(
            message.contains("missing interpreter slot"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn audit_packet_matches_run_program_fills_on_synthetic_nq_slots() {
        let qty_one = r#"{"type":"number","value":"1"}"#;
        let qty_all = "null";
        let body = format!(
            "{},{},{}",
            nq_slot_var_decls(),
            order_if_bar(2, "buy", "LE", qty_one),
            order_if_bar(6, "sell", "LX", qty_all),
        );
        let ir = wrap_ir(&body);
        let tape = load_csv(sample_bars_path().to_str().unwrap());
        let baseline = run_program(&ir, &tape, None, &nq_config(), &HashMap::new())
            .unwrap_or_else(|e| panic!("baseline: {e}"));
        let (audited, packet) =
            run_program_audit_packet(&ir, &tape, None, &nq_config(), &HashMap::new(), false)
                .unwrap_or_else(|e| panic!("audit: {e}"));
        assert_eq!(baseline.fills, audited.fills);
        assert_eq!(baseline.skipped_bars, audited.skipped_bars);
        assert!(packet.len() >= crate::audit::HEADER_SIZE);
        assert_eq!(&packet[0..8], crate::audit::MAGIC);
        let trans = u32::from_le_bytes(packet[20..24].try_into().unwrap());
        let trades = u32::from_le_bytes(packet[24..28].try_into().unwrap());
        let fills = u32::from_le_bytes(packet[48..52].try_into().unwrap());
        let remaining = u32::from_le_bytes(packet[60..64].try_into().unwrap());
        assert_eq!(fills as usize, baseline.fills.len());
        assert_eq!(remaining, 0);
        assert!(trans >= 2, "expected scale-in and full-exit, got {trans}");
        assert_eq!(trades, 1);
        let expected_len = crate::audit::HEADER_SIZE
            + trans as usize * crate::audit::TRANSITION_SIZE
            + trades as usize * crate::audit::TRADE_SIZE;
        assert_eq!(packet.len(), expected_len);
    }

    fn tape_with_dates(dates: &[i64]) -> BarTape {
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

    fn slice_tape(tape: &BarTape, from: usize) -> BarTape {
        BarTape::from_columns(
            tape.opens()[from..].to_vec(),
            tape.highs()[from..].to_vec(),
            tape.lows()[from..].to_vec(),
            tape.closes()[from..].to_vec(),
            tape.volumes()[from..].to_vec(),
            tape.dates()[from..].to_vec(),
            tape.times()[from..].to_vec(),
        )
        .unwrap()
    }

    #[test]
    fn slice_before_marshal_sliced_equals_full_packet() {
        let buy = r#"{"type":"order","action":"buy","bar_timing":"next","entry_label":null,"label":{"type":"string","value":"LE"},"order_type":"market","price":null,"quantity":{"type":"number","value":"1"}}"#;
        let body = format!(
            r#"{{"type":"input_decl","decls":[{{"name":"firsttradedate","default":{{"type":"number","value":"1180212"}}}}]}},{},{{"type":"if","then_block":true,"else_body":null,"cond":{{"type":"compare","op":">=","left":{{"type":"ident","name":"date"}},"right":{{"type":"ident","name":"firsttradedate"}}}},"body":[{buy}]}}"#,
            nq_slot_var_decls(),
        );
        let ir = wrap_ir(&body);
        let mut dates = Vec::new();
        for d in [1180101i64, 1180102, 1180211, 1180212, 1180213] {
            for _ in 0..3 {
                dates.push(d);
            }
        }
        let tape = tape_with_dates(&dates);

        let plan = skip_eval_from(&ir, &dates, &HashMap::new()).expect("skip plan");
        assert!(plan.proven, "{}", plan.reason);
        assert!(plan.eval_from > 0, "{}", plan.reason);

        let (full_outcome, full_packet) =
            run_program_audit_packet(&ir, &tape, None, &nq_config(), &HashMap::new(), false)
                .expect("full packet");
        assert_eq!(full_outcome.skipped_bars, plan.eval_from);

        // Slice before marshal: run over tape[eval_from..] with the offset.
        let slice = slice_tape(&tape, plan.eval_from);
        let sliced = run_program_audit_packet_sliced(
            &ir,
            &slice,
            None,
            &nq_config(),
            &HashMap::new(),
            plan.eval_from,
            tape.len(),
        )
        .expect("sliced packet");
        assert_eq!(sliced, full_packet, "sliced packet must equal the full one");

        // And the batch over one variant reproduces it too.
        let batch = run_program_audit_packet_batch(
            &ir,
            &slice,
            None,
            &nq_config(),
            &[HashMap::new()],
            plan.eval_from,
            tape.len(),
        )
        .expect("batch packet");
        assert_eq!(batch.len(), 1);
        assert_eq!(batch[0], full_packet);
    }

    /// `TLID = TL_New(...)` with `TLID` unread and no orders. Dead-store
    /// elimination and the no-order skip must both leave the trendline.
    #[test]
    fn unread_tl_new_records_one_object() {
        let ir = r#"{"kind":"pl_transpiler.ast","ir_version":1,"ast":{"type":"program","body":[
            {"type":"var_decl","decls":[{"name":"tlid","init":{"type":"unaryop","op":"-","operand":{"type":"number","value":"1"}}}]},
            {"type":"assign","target":{"type":"ident","name":"tlid"},"value":{"type":"call","name":"tl_new","args":[
                {"type":"ident","name":"date"},{"type":"ident","name":"time"},{"type":"ident","name":"low"},
                {"type":"ident","name":"date"},{"type":"ident","name":"time"},{"type":"ident","name":"high"}
            ]}}
        ]}}"#;
        let tape = BarTape::from_columns(
            vec![10.25],
            vec![12.0],
            vec![9.0],
            vec![11.0],
            vec![1.0],
            vec![1260102],
            vec![1002],
        )
        .unwrap();
        let (_fills, raw) =
            run_program_drawings(ir, &tape, None, &nq_config(), &HashMap::new()).expect("draw");
        let objs: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let objs = objs.as_array().expect("array");
        assert_eq!(objs.len(), 1, "{raw}");
        assert_eq!(objs[0]["type"], "tl");
        assert_eq!(objs[0]["color"], 36095);
        assert_eq!(objs[0]["style"], 1);
        assert_eq!(objs[0]["anchors"][0]["bn"], 1);
        assert_eq!(objs[0]["anchors"][0]["price_ticks"], 36);
        assert_eq!(objs[0]["anchors"][1]["price_ticks"], 48);
        assert_eq!(objs[0]["created"]["bn"], 1);
    }
}
