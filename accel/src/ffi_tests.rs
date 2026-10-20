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

//! FFI-layer tests for the `run_program` seam in `lib.rs`.
//!
//! These exercise the pure-Rust marshaling core ([`super::run_program_core`])
//! behind the `run_program` pyfunction. They deliberately avoid the embedded
//! Python interpreter: the crate builds with `pyo3/extension-module`, which
//! does not link `libpython`, so `Python::with_gil` segfaults under
//! `cargo test`. The Python-object extraction (`extract_config` /
//! `extract_tape`) is a thin extract-and-call layer covered end-to-end by the
//! import + `run_program` parity check driven from Python (see
//! `tools/build_accel.sh` and `ACCEL-INTERFACE-V2.md`).

use std::fs;
use std::path::PathBuf;

use crate::types::{BarTape, Config, Session};

use super::{
    run_program_batch_core, run_program_batch_parallel_core, run_program_core, run_variant_core,
    KernelError,
};

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
        warmup_seed: 0,
        symccy: 1,
        symnm: "@NQ#C".into(),
        symalt: "@NQ#C".into(),
        descstr: "E-MINI NASDAQ 100".into(),
        expdate: 1130331,
    }
}

fn empty_tape() -> BarTape {
    BarTape::from_columns(
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
    )
    .expect("empty columns are equal length")
}

fn sample_bars_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../examples/NQ_sample_bars.csv")
}

fn load_gt2_tape() -> BarTape {
    let text = fs::read_to_string(sample_bars_path()).expect("read examples/NQ_sample_bars.csv");
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
        date.push(cols[0].trim().parse().expect("date"));
        time.push(cols[1].trim().parse().expect("time"));
        open.push(cols[2].trim().parse().expect("open"));
        high.push(cols[3].trim().parse().expect("high"));
        low.push(cols[4].trim().parse().expect("low"));
        close.push(cols[5].trim().parse().expect("close"));
        volume.push(cols[6].trim().parse().expect("volume"));
    }
    BarTape::from_columns(open, high, low, close, volume, date, time).expect("valid tape")
}

/// The FFI seam produces the same four fills the Python reference reports for
/// the checked-in GT2 IR over the 140-bar sample tape.
#[test]
fn run_program_core_matches_python_gt2_fills() {
    let ir = include_str!("strategy/fixtures/gt2.ir.json");
    let tape = load_gt2_tape();
    assert_eq!(tape.len(), 140, "NQ_sample_bars.csv bar count");

    let fills =
        run_program_core(ir, &tape, None, &nq_config()).expect("native run_program_core failed");

    let expected: &[(&str, &str, i64, i64, f64)] = &[
        ("ExitLong", "SE", 1240102, 1001, 15028.5),
        ("EntryLong", "LE", 1240102, 1034, 14967.14),
        ("ExitLong", "SE", 1240102, 1109, 15017.75),
        ("EntryLong", "LE", 1240102, 1144, 14985.45),
    ];
    assert_eq!(
        fills.len(),
        expected.len(),
        "fill count: native={} python={}",
        fills.len(),
        expected.len()
    );
    for (i, (fill, (ty, sig, date, time, px))) in fills.iter().zip(expected.iter()).enumerate() {
        assert_eq!(fill.fill_type, *ty, "fill {i} type");
        assert_eq!(fill.signal, *sig, "fill {i} signal");
        assert_eq!(fill.date, *date, "fill {i} date");
        assert_eq!(fill.time, *time, "fill {i} time");
        assert_eq!(fill.contracts, 1, "fill {i} contracts");
        assert_eq!(
            fill.price.to_bits(),
            (*px).to_bits(),
            "fill {i} price native={} python={}",
            fill.price,
            px
        );
    }
}

/// A program_ir that references Data2 is refused loudly when no Data2 series is
/// supplied — never approximated.
#[test]
fn run_program_core_refuses_data2_program() {
    let ir = r#"{"ir_version":1,"kind":"pl_transpiler.ast","ast":{"type":"program","body":[{"type":"assign","target":{"type":"ident","name":"value1"},"value":{"type":"data_of","series":"close","data_num":2}}]}}"#;
    let err = run_program_core(ir, &empty_tape(), None, &nq_config()).expect_err("must refuse");
    assert!(
        err.to_string().contains("Data2"),
        "refusal names Data2: {err}"
    );
}

/// A supplied Data2 series is refused loudly (Data2 is not yet implemented).
#[test]
fn run_program_core_refuses_supplied_data2() {
    let ir = include_str!("strategy/fixtures/gt2.ir.json");
    let tape = empty_tape();
    let err = run_program_core(ir, &tape, Some(&tape), &nq_config()).expect_err("must refuse");
    assert!(
        err.to_string().contains("Data2"),
        "refusal names Data2: {err}"
    );
}

/// Malformed program_ir raises an input error, not a panic or a wrong result.
#[test]
fn run_program_core_refuses_malformed_ir() {
    let err = run_program_core("not json", &empty_tape(), None, &nq_config())
        .expect_err("must refuse malformed IR");
    let message = err.to_string();
    assert!(message.contains("malformed program_ir"), "got: {message}");
}

/// The failure classification the pyfunction relies on: input errors carry the
/// `invalid accel input:` prefix, everything else the unsupported/strategy
/// class (mapped to `NotImplementedError` at the boundary).
#[test]
fn kernel_error_classification_prefixes_are_stable() {
    let input = KernelError::input("boom");
    assert!(input.to_string().starts_with("invalid accel input:"));
    let unsupported = KernelError::unsupported("boom");
    assert!(!unsupported.to_string().starts_with("invalid accel input:"));
    let strategy = KernelError::strategy("boom");
    assert!(!strategy.to_string().starts_with("invalid accel input:"));
}

/// The sweep invariant: a batch of variants is per-variant bit-identical to a
/// solo run with the same inputs, and the default (`{}`) variant equals the
/// plain `run_program` path. The parallel batch is identical to the sequential
/// batch — parallelism must never change an answer.
#[test]
fn batch_variants_match_solo_and_parallel_matches_sequential() {
    use std::collections::HashMap;

    let ir = include_str!("strategy/fixtures/gt2.ir.json");
    let tape = load_gt2_tape();
    let cfg = nq_config();

    let default = run_program_core(ir, &tape, None, &cfg).expect("default run");

    let variants: Vec<HashMap<String, crate::strategy::Value>> = vec![
        HashMap::new(),
        // Source spelling (GT2 writes FastLen) — must apply, not silently miss.
        [("FastLen".to_string(), crate::strategy::Value::Num(20.0))].into(),
        [("slowlen".to_string(), crate::strategy::Value::Num(60.0))].into(),
    ];

    let batch = run_program_batch_core(ir, &tape, None, &cfg, &variants).expect("batch run");
    let par = run_program_batch_parallel_core(ir, &tape, None, &cfg, &variants, 2)
        .expect("parallel batch run");

    assert_eq!(batch.len(), variants.len());
    assert_eq!(par.len(), variants.len());

    // the default variant is the plain run_program path, bit-for-bit.
    assert_eq!(batch[0], default);
    assert_eq!(par[0], default);

    // each variant matches its solo run, bit-for-bit.
    for (i, variant) in variants.iter().enumerate() {
        let solo = run_variant_core(ir, &tape, None, &cfg, variant).expect("solo run");
        assert_eq!(batch[i], solo, "variant {i} batch != solo");
        assert_eq!(par[i], solo, "variant {i} parallel != solo");
    }

    // parallel == sequential, bit-for-bit.
    assert_eq!(batch, par);

    // an overridden input actually perturbs the strategy (fast maths change
    // the cross -> a different trade list), so the invariant is non-vacuous.
    let variant = &variants[1];
    let solo = run_variant_core(ir, &tape, None, &cfg, variant).unwrap_or_default();
    assert_ne!(solo, default, "FastLen=20 must change the trade list");

    // Folded-key and source-spelling overrides are the same variant.
    let folded: HashMap<String, crate::strategy::Value> =
        [("fastlen".to_string(), crate::strategy::Value::Num(20.0))].into();
    let folded_solo = run_variant_core(ir, &tape, None, &cfg, &folded).expect("folded FastLen");
    assert_eq!(
        folded_solo, solo,
        "FastLen and fastlen must resolve identically"
    );
}
