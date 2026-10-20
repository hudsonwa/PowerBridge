// Temporary benchmark harness (issue #9 profiling). Not part of the shipped
// test surface; wire via `#[cfg(test)] mod bench;` in strategy/mod.rs.

use std::collections::HashMap;
use std::time::Instant;

use crate::types::{BarTape, Config, Session};

use super::run_program;
use super::value::Value;

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

fn synth_tape(n: usize) -> BarTape {
    let mut open = Vec::with_capacity(n);
    let mut high = Vec::with_capacity(n);
    let mut low = Vec::with_capacity(n);
    let mut close = Vec::with_capacity(n);
    let mut volume = Vec::with_capacity(n);
    let mut date = Vec::with_capacity(n);
    let mut time = Vec::with_capacity(n);
    let mut price = 18902.25f64;
    let mut seed = 0x12345678u64;
    let mut next = move || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        (seed % 1000) as f64 / 1000.0
    };
    let mut day = 1240102i64;
    let mut minute = 831i64;
    for _ in 0..n {
        let drift = (next() - 0.5) * 0.5;
        price = (price + drift).max(1.0);
        let o = price;
        let h = (price + next() * 0.5).max(o);
        let l = (price - next() * 0.5).max(0.0);
        let c = (l + next() * (h - l)).max(0.0);
        open.push(o);
        high.push(h);
        low.push(l);
        close.push(c);
        volume.push(1000.0 + next() * 5000.0);
        date.push(day);
        time.push(minute);
        minute += 1;
        if minute > 1515 {
            minute = 831;
            day += 1;
            if day % 100 > 28 {
                day = (day / 100) * 100 + 101;
            }
        }
        price = c;
    }
    BarTape::from_columns(open, high, low, close, volume, date, time).unwrap()
}

fn bench_ir(label: &str, ir: &str, tape: &BarTape) {
    let start = Instant::now();
    let outcome = run_program(
        ir,
        tape,
        None,
        &nq_config(),
        &HashMap::<String, Value>::new(),
    )
    .unwrap_or_else(|e| panic!("{label} failed: {e}"));
    let elapsed = start.elapsed();
    eprintln!(
        "[bench] {label}: {} bars in {:.3}s ({:.3} us/bar), {} fills",
        tape.len(),
        elapsed.as_secs_f64(),
        elapsed.as_secs_f64() * 1e6 / tape.len() as f64,
        outcome.fills.len()
    );
}

fn bench_compile_and_loop(label: &str, ir: &str, tape: &BarTape) {
    let inputs = HashMap::<String, Value>::new();
    let compile_start = Instant::now();
    let ast = super::ir::load_program_ir(ir).expect("load IR");
    let data2 = if super::ir::ast_uses_data2(&ast) {
        Some(std::sync::Arc::new(super::interp::align_data2(tape, tape)))
    } else {
        None
    };
    let mut strategy = super::interp::Interpreter::new(ast, &inputs, data2).expect("compile");
    let compile_us = compile_start.elapsed().as_secs_f64() * 1e6;
    let spec = strategy.specialize_stats();
    let specialize_us = spec.specialize_ns as f64 / 1e3;

    let loop_start = Instant::now();
    let config = nq_config();
    let outcome = crate::kernel::run_native(tape, (&config).into(), &mut strategy)
        .unwrap_or_else(|e| panic!("{label} failed: {e}"));
    let loop_elapsed = loop_start.elapsed();
    eprintln!(
        "[bench] {label}: compile {:.1} us (specialise {:.1} us, {} -> {} instrs); loop {} bars in {:.3}s ({:.3} us/bar), {} fills",
        compile_us,
        specialize_us,
        spec.instrs_before,
        spec.instrs_after,
        tape.len(),
        loop_elapsed.as_secs_f64(),
        loop_elapsed.as_secs_f64() * 1e6 / tape.len() as f64,
        outcome.fills.len()
    );
}

const GT2_IR: &str = include_str!("fixtures/gt2.ir.json");

// A Close-of-Data1 minimal strategy (no series functions, no formatting).
const MINIMAL_IR: &str = r#"{
  "ir_version": 1,
  "kind": "pl_transpiler.ast",
  "ast": {
    "type": "program",
    "body": [
      {"type": "if", "cond": {"type": "compare", "op": ">", "left": {"type": "ident", "name": "close"}, "right": {"type": "number", "value": "0"}}, "body": [{"type": "order", "action": "buy", "order_type": "market", "bar_timing": "next", "label": {"type": "string", "value": "LE"}, "quantity": {"type": "number", "value": "1"}}], "else_body": null}
    ]
  }
}"#;

#[test]
#[ignore]
fn bench_large() {
    let n = 1_000_000;
    let tape = synth_tape(n);
    bench_ir("minimal 1M", MINIMAL_IR, &tape);
    bench_ir("gt2 1M", GT2_IR, &tape);
}

#[test]
#[ignore]
fn bench_small() {
    let tape = synth_tape(200_000);
    bench_ir("minimal 200k", MINIMAL_IR, &tape);
    bench_ir("gt2 200k", GT2_IR, &tape);
}

#[test]
#[ignore]
fn bench_compile_cost() {
    let tape = synth_tape(200_000);
    bench_compile_and_loop("gt2 200k (compile/loop split)", GT2_IR, &tape);
    bench_compile_and_loop("minimal 200k (compile/loop split)", MINIMAL_IR, &tape);
}

#[test]
#[ignore]
fn bench_scale() {
    for n in [10_000usize, 20_000, 50_000, 100_000] {
        let tape = synth_tape(n);
        bench_ir(&format!("gt2 {n}"), GT2_IR, &tape);
    }
}

// A zero-lookback "audit"-shaped program: scalar arithmetic on Data-of-Data2
// plus a long incrementally-built string line. No series functions, no
// lookback — this is the shape Audit Ref 3 (zero lookback) presents.
#[test]
#[ignore]
fn bench_zerolookback() {
    use serde_json::json;
    let mut body = Vec::new();
    body.push(json!({"type":"var_decl","decls":[{"name":"V1","init":{"type":"number","value":"0"}},{"name":"V2","init":{"type":"number","value":"0"}},{"name":"V3","init":{"type":"number","value":"0"}},{"name":"Line","init":{"type":"string","value":""}}]}));
    for i in 0..12 {
        let v = format!("V{}", i % 3 + 1);
        // V(n) = (D2C + Close) * 2 - High + Low
        let expr = json!({"type":"binop","op":"-","left":{"type":"binop","op":"+","left":{"type":"binop","op":"*","left":{"type":"binop","op":"+","left":{"type":"ident","name":"close"},"right":{"type":"ident","name":"close"}},"right":{"type":"number","value":"2"}},"right":{"type":"ident","name":"high"}},"right":{"type":"ident","name":"low"}});
        body.push(json!({"type":"assign","target":{"type":"ident","name":v},"value":expr}));
    }
    body.push(json!({"type":"assign","target":{"type":"ident","name":"Line"},"value":{"type":"binop","op":"+","left":{"type":"binop","op":"+","left":{"type":"call","name":"numtostr","args":[{"type":"ident","name":"close"},{"type":"number","value":"4"}]},"right":{"type":"string","value":","}},"right":{"type":"call","name":"numtostr","args":[{"type":"ident","name":"V1"},{"type":"number","value":"4"}]}}}));
    body.push(json!({"type":"if","cond":{"type":"compare","op":"<","left":{"type":"ident","name":"V1"},"right":{"type":"ident","name":"V2"}},"body":[{"type":"order","action":"buy","order_type":"market","bar_timing":"next","label":{"type":"string","value":"LE"},"quantity":{"type":"number","value":"1"}}],"else_body":null}));
    body.push(json!({"type":"print","args":[{"type":"string","value":"out"},{"type":"ident","name":"Line"}]}));
    let ir =
        json!({"ir_version":1,"kind":"pl_transpiler.ast","ast":{"type":"program","body":body}})
            .to_string();

    let n = 200_000;
    let tape = synth_tape(n);
    bench_ir(&format!("zerolookback {n}"), &ir, &tape);
}
