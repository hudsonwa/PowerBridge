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

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value as Json;

use super::run_program;
use super::value::{canonical_fill_line, Value};
use crate::types::{BarTape, Config, Session};

const PYTHON_REFERENCE: &str = r#"
import json
import sys

from pl_transpiler import tokenise
from pl_transpiler.ir import serialise_ir
from pl_transpiler.parser import parse
from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_oracle import canonical_fill_line, run_oracle
from pl_transpiler.tools.pl_run import load_bars_csv

source = sys.stdin.read()
tokens, positions = tokenise(source)
ast = parse(tokens, positions=positions, source=source)
bars = load_bars_csv(sys.argv[1])[int(sys.argv[2]):int(sys.argv[3])]
data2_closes = None
if len(sys.argv) >= 5:
    d2 = load_bars_csv(sys.argv[4])
    by_key = {(r["date"], r["time"]): r["close"] for r in d2}
    data2_closes = [by_key.get((b["date"], b["time"]), b["close"]) for b in bars]
result = run_oracle(source, bars, get_config("NQ"), data2_closes=data2_closes)
print(json.dumps({
    "ir": serialise_ir(ast),
    "fills": [canonical_fill_line(fill) for fill in result["records"]],
}, sort_keys=True))
"#;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("accel has a repository parent")
        .to_path_buf()
}

fn sample_bars_path() -> PathBuf {
    repo_root().join("examples/NQ_sample_bars.csv")
}

/// Committed Data2 tape fixture: the full 140-bar `NQ_sample_bars.csv` stream
/// with every close offset by `+3`. The `(date, time)` columns are verbatim, so
/// the loader aligns each Data2 bar back onto its source Data1 bar.
fn data2_tape_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("src/strategy/fixtures/data2_ref3_shape_tape.csv")
}

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

fn load_csv_window(path: &Path, start: usize, end: usize) -> BarTape {
    let text =
        fs::read_to_string(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    let mut open = Vec::new();
    let mut high = Vec::new();
    let mut low = Vec::new();
    let mut close = Vec::new();
    let mut volume = Vec::new();
    let mut date = Vec::new();
    let mut time = Vec::new();
    for (index, line) in text
        .lines()
        .skip(1)
        .filter(|line| !line.trim().is_empty())
        .enumerate()
    {
        if index < start || index >= end {
            continue;
        }
        let columns: Vec<&str> = line.split(',').collect();
        assert!(columns.len() >= 7, "bad CSV row {index}: {line}");
        date.push(columns[0].trim().parse().expect("date"));
        time.push(columns[1].trim().parse().expect("time"));
        open.push(columns[2].trim().parse().expect("open"));
        high.push(columns[3].trim().parse().expect("high"));
        low.push(columns[4].trim().parse().expect("low"));
        close.push(columns[5].trim().parse().expect("close"));
        volume.push(columns[6].trim().parse().expect("volume"));
    }
    BarTape::from_columns(open, high, low, close, volume, date, time)
        .expect("CSV window makes a valid tape")
}

fn python_reference(source: &str, start: usize, end: usize) -> (String, Vec<String>) {
    let path = sample_bars_path();
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(PYTHON_REFERENCE)
        .arg(&path)
        .arg(start.to_string())
        .arg(end.to_string())
        .current_dir(repo_root())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn Python reference");
    child
        .stdin
        .take()
        .expect("Python stdin")
        .write_all(source.as_bytes())
        .expect("write EL source");
    let output = child.wait_with_output().expect("wait for Python reference");
    assert!(
        output.status.success(),
        "Python reference failed with {}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Json = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|error| panic!("decode Python reference JSON: {error}"));
    let ir = result["ir"]
        .as_str()
        .expect("reference IR string")
        .to_owned();
    let fills = result["fills"]
        .as_array()
        .expect("reference fill list")
        .iter()
        .map(|line| line.as_str().expect("canonical fill line").to_owned())
        .collect();
    (ir, fills)
}

fn python_reference_data2(
    source: &str,
    start: usize,
    end: usize,
    data2: &BarTape,
) -> (String, Vec<String>) {
    let path = sample_bars_path();
    let d2_path = write_bars_csv(data2);
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(PYTHON_REFERENCE)
        .arg(&path)
        .arg(start.to_string())
        .arg(end.to_string())
        .arg(&d2_path)
        .current_dir(repo_root())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn Python reference");
    child
        .stdin
        .take()
        .expect("Python stdin")
        .write_all(source.as_bytes())
        .expect("write EL source");
    let output = child.wait_with_output().expect("wait for Python reference");
    let _ = fs::remove_file(&d2_path);
    assert!(
        output.status.success(),
        "Python reference failed with {}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Json = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|error| panic!("decode Python reference JSON: {error}"));
    let ir = result["ir"]
        .as_str()
        .expect("reference IR string")
        .to_owned();
    let fills = result["fills"]
        .as_array()
        .expect("reference fill list")
        .iter()
        .map(|line| line.as_str().expect("canonical fill line").to_owned())
        .collect();
    (ir, fills)
}

fn check_python_native_parity(context: &str, source: &str, start: usize, end: usize) -> String {
    let (ir, python_fills) = python_reference(source, start, end);
    let tape = load_csv_window(&sample_bars_path(), start, end);
    let outcome = run_program(
        &ir,
        &tape,
        None,
        &nq_config(),
        &HashMap::<String, Value>::new(),
    )
    .unwrap_or_else(|error| {
        panic!("{context}: native execution failed while Python produced {python_fills:?}: {error}")
    });
    let native_fills: Vec<String> = outcome.fills.iter().map(canonical_fill_line).collect();
    assert_eq!(
        native_fills, python_fills,
        "{context}: whole canonical fill lines differ"
    );
    ir
}

fn assert_python_native_parity(context: &str, source: &str, start: usize, end: usize) {
    let _fresh_ir = check_python_native_parity(context, source, start, end);
}

/// Build a Data2 tape from a contiguous window of Data1 bars, offsetting the
/// close by `close_delta` so the second stream is observably different. The
/// date/time columns are copied verbatim, so `(date, time)` alignment maps each
/// Data2 bar back onto its source Data1 bar.
fn make_data2_tape(base: &BarTape, close_delta: f64, from: usize, to: usize) -> BarTape {
    let mut open = Vec::new();
    let mut high = Vec::new();
    let mut low = Vec::new();
    let mut close = Vec::new();
    let mut volume = Vec::new();
    let mut date = Vec::new();
    let mut time = Vec::new();
    for i in from..to.min(base.len()) {
        let bar = base.bar(i);
        open.push(bar.open);
        high.push(bar.high);
        low.push(bar.low);
        close.push(bar.close + close_delta);
        volume.push(bar.volume);
        date.push(bar.date);
        time.push(bar.time);
    }
    BarTape::from_columns(open, high, low, close, volume, date, time).expect("valid Data2 tape")
}

fn write_bars_csv(tape: &BarTape) -> PathBuf {
    static NEXT_ID: AtomicUsize = AtomicUsize::new(0);
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!("accel_d2_{}_{}.csv", std::process::id(), id));
    let mut text = String::from("date,time,open,high,low,close,volume\n");
    for i in 0..tape.len() {
        let bar = tape.bar(i);
        text.push_str(&format!(
            "{},{},{},{},{},{},{}\n",
            bar.date, bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume
        ));
    }
    fs::write(&path, text).unwrap_or_else(|error| panic!("write {}: {error}", path.display()));
    path
}

/// Run the native interpreter over a Data1 window plus a supplied Data2 tape and
/// assert the canonical fill lines are byte-identical to Python.
fn check_data2_parity(context: &str, source: &str, start: usize, end: usize, data2: &BarTape) {
    let (ir, python_fills) = python_reference_data2(source, start, end, data2);
    let tape = load_csv_window(&sample_bars_path(), start, end);
    let outcome = run_program(
        &ir,
        &tape,
        Some(data2),
        &nq_config(),
        &HashMap::<String, Value>::new(),
    )
    .unwrap_or_else(|error| {
        panic!("{context}: native execution failed while Python produced {python_fills:?}: {error}")
    });
    let native_fills: Vec<String> = outcome.fills.iter().map(canonical_fill_line).collect();
    assert_eq!(
        native_fills, python_fills,
        "{context}: whole canonical fill lines differ"
    );
}

#[test]
fn gt2_fixture_and_fills_match_fresh_python_reference() {
    let source_path = repo_root().join("ground_truth/GT2_strategy_orders_position.txt");
    let source = fs::read_to_string(&source_path)
        .unwrap_or_else(|error| panic!("read {}: {error}", source_path.display()));
    let fresh_ir = check_python_native_parity("GT2 140-bar sample", &source, 0, 140);
    assert_eq!(
        fresh_ir.as_bytes(),
        include_bytes!("fixtures/gt2.ir.json"),
        "checked-in GT2 IR must be byte-identical to a fresh serialise_ir"
    );
}

#[test]
fn data_series_short_history_matches_python_across_lengths_and_windows() {
    let scenarios = [
        ("Close", 1, 0, 7),
        ("High", 2, 3, 11),
        ("Low", 4, 8, 18),
        ("Open", 3, 1, 10),
        ("Volume", 5, 13, 25),
    ];
    for (series, lookback, start, end) in scenarios {
        let source = format!(
            "Inputs: BarsAgo({lookback});\n\
             If {series}[BarsAgo] > 0 Then\n\
                 Buy (\"LB\") 1 Contracts Next Bar At Market;\n"
        );
        assert_python_native_parity(
            &format!("{series}[{lookback}] over bars {start}..{end}"),
            &source,
            start,
            end,
        );
    }
}

#[test]
fn series_lookback_reads_raw_price_like_python() {
    // Python reads `close[-2]` unrounded; f32(Close[1]) fired this on bar 3.
    let source = "If BarNumber = 3 and Close - Close[1] > 8.179648437499964 Then\n\
                  Buy (\"LB\") 1 Contracts Next Bar At Market;\n";
    assert_python_native_parity("Close - Close[1] near an f32 boundary", source, 0, 20);
}

#[test]
fn variable_short_history_uses_python_initializer_fallback() {
    for (lookback, start, end) in [(2, 0, 8), (3, 4, 13), (5, 9, 21)] {
        let source = format!(
            "Inputs: BarsAgo({lookback});\n\
             Variables: Trend(1);\n\
             If Trend[BarsAgo] = 1 Then\n\
                 Buy (\"VAR\") 1 Contracts Next Bar At Market;\n\
             Trend = 2;\n"
        );
        assert_python_native_parity(
            &format!("Trend[{lookback}] over bars {start}..{end}"),
            &source,
            start,
            end,
        );
    }
}

#[test]
fn second_once_matches_python_first_bar_consumption() {
    let source = "Once Buy (\"FIRST\") 1 Contracts Next Bar At Market;\n\
                  Once Buy (\"SECOND\") 1 Contracts Next Bar At Market;\n";
    assert_python_native_parity("two reachable Once statements", source, 2, 8);
}

#[test]
fn fractional_average_lengths_match_python_before_slice_is_reached() {
    for (length, active_bars, start, end) in
        [("1.5", 1, 0, 6), ("2.5", 2, 5, 12), ("3.5", 3, 11, 20)]
    {
        let source = format!(
            "Inputs: Period({length});\n\
             Variables: Value1(0);\n\
             If CurrentBar <= {active_bars} Then Begin\n\
                 Value1 = Average(Close, Period);\n\
                 If Value1 > 0 Then\n\
                     Buy (\"AVG\") 1 Contracts Next Bar At Market;\n\
             End;\n"
        );
        assert_python_native_parity(
            &format!("Average length {length} over bars {start}..{end}"),
            &source,
            start,
            end,
        );
    }
}

#[test]
fn len_and_prec_inputs_keep_their_own_defaults() {
    let scenarios = [
        (
            "Len",
            "Inputs: Len(1);\n\
             If Len = 1 Then Buy (\"LEN\") 1 Contracts Next Bar At Market;\n",
        ),
        (
            "Prec",
            "Inputs: Prec(1);\n\
             If Prec = 1 Then Buy (\"PREC\") 1 Contracts Next Bar At Market;\n",
        ),
    ];
    for (name, source) in scenarios {
        assert_python_native_parity(&format!("Input named {name}"), source, 3, 10);
    }
}

#[test]
fn unary_minus_input_default_matches_python() {
    let source = "Inputs: Qty(-1);\n\
                  If Qty = -1 Then Buy (\"NEG\") 1 Contracts Next Bar At Market;\n";
    assert_python_native_parity("unary-minus Input default", source, 7, 14);
}

#[test]
fn average_of_variable_matches_python_numeric_history() {
    // `Average(Up, 3)` makes `Up` a NumericVar series source, so its `num_history`
    // (not the value history) is what Average reads. Must stay bit-exact after
    // the per-slot history gating.
    let source = "Variables: Up(0), MA(0);\n\
                  Up = Close + 0.25;\n\
                  MA = Average(Up, 3);\n\
                  If MA > 0 Then Buy (\"AVG\") 1 Contracts Next Bar At Market;\n";
    assert_python_native_parity("Average(Up,3) variable series", source, 0, 40);
}

#[test]
fn crosses_of_variables_match_python_numeric_history() {
    // Both crossing arms take numeric variables as series sources; the fill list
    // must agree byte-for-byte. Exercises two NumericVar sources at once.
    let source = "Variables: Fast(0), Slow(0);\n\
                  Fast = Average(Close, 2);\n\
                  Slow = Average(Close, 5);\n\
                  If Fast Crosses Above Slow Then Buy (\"XAB\") 1 Contracts Next Bar At Market;\n\
                  If Fast Crosses Below Slow Then SellShort (\"XBW\") 1 Contracts Next Bar At Market;\n";
    assert_python_native_parity("two-variable crosses", source, 0, 140);
}

// ---- Data2 (`Close of Data2` / `Close of Data(2)`) parity ----

/// Reference-strategy-shaped Data2 program: reads `Close of Data2`, `Close of Data(2)`,
/// `High of Data2` and `Low of Data2` into numeric variables, then trades on the
/// Data2 close. The limit price is the *narrowed* `D2C`, so the f32 narrowing
/// point on the Data2 path is bit-exercised in every fill price, exactly like
/// Python's `d2c = f32(kwargs.get('data2_close', close[-1]))`. The source lives
/// in a committed fixture so later code references it by path instead of
/// re-inlining it.
const DATA2_FIXTURE: &str = include_str!("fixtures/data2_ref3_shape.txt");

fn clone_tape_with_close_overrides(base: &BarTape, overrides: &[(usize, f64)]) -> BarTape {
    let mut open = Vec::new();
    let mut high = Vec::new();
    let mut low = Vec::new();
    let mut close = Vec::new();
    let mut volume = Vec::new();
    let mut date = Vec::new();
    let mut time = Vec::new();
    for i in 0..base.len() {
        let bar = base.bar(i);
        open.push(bar.open);
        high.push(bar.high);
        low.push(bar.low);
        let mut c = bar.close;
        for &(idx, delta) in overrides {
            if i == idx {
                c += delta;
            }
        }
        close.push(c);
        volume.push(bar.volume);
        date.push(bar.date);
        time.push(bar.time);
    }
    BarTape::from_columns(open, high, low, close, volume, date, time).expect("valid perturbed tape")
}

#[test]
fn data2_close_of_data2_matches_python_bit_exactly() {
    // Full second bar stream: every Data1 bar has a matching Data2 bar, its
    // close offset by +3 so the Data2-derived limit order fills intra-bar at the
    // (f32-narrowed) Data2 close — proving the whole `data_of` path bit-exactly.
    let data2 = load_csv_window(&data2_tape_path(), 0, 140);
    check_data2_parity("Data2 full 140-bar stream", DATA2_FIXTURE, 0, 140, &data2);
}

#[test]
fn data2_shorter_than_data1_matches_python_leading_and_trailing_gap() {
    // Data2 covers only Data1 bars [15, 60): a leading gap (bars 0-14) and a
    // trailing gap (bars 60-139). `(date, time)` alignment must map the Data2 bars
    // back onto bars 15-59 and fall back to the primary close everywhere else —
    // index alignment would pin Data2[0] onto Data1[0] and diverge.
    let tape = load_csv_window(&sample_bars_path(), 0, 140);
    let data2 = make_data2_tape(&tape, 3.0, 15, 60);
    check_data2_parity(
        "Data2 shorter than Data1 (leading + trailing gap)",
        DATA2_FIXTURE,
        0,
        140,
        &data2,
    );
}

#[test]
fn data2_perturbation_changes_fills() {
    let source = DATA2_FIXTURE;
    let tape = load_csv_window(&sample_bars_path(), 0, 140);
    let data2 = make_data2_tape(&tape, 3.0, 0, 140);
    let (ir, python_fills) = python_reference_data2(source, 0, 140, &data2);

    // Perturb one Data2 close so that bar's signal (`D2C > Close`) flips off:
    // its close drops below the primary close, so that bar places no order and
    // the fill list shrinks by one. An interpreter that ignored Data2 would not
    // change at all, so this assert has teeth against a dropped Data2 value.
    let perturbed = clone_tape_with_close_overrides(&data2, &[(20, -6.0)]);
    let outcome = run_program(
        &ir,
        &tape,
        Some(&perturbed),
        &nq_config(),
        &HashMap::<String, Value>::new(),
    )
    .expect("perturbed Data2 run");
    let native_fills: Vec<String> = outcome.fills.iter().map(canonical_fill_line).collect();
    assert_ne!(
        native_fills, python_fills,
        "perturbing a Data2 close must change the canonical fill list"
    );
}

#[test]
fn data2_supplied_without_data2_reference_is_refused() {
    // The unsupported-dialect refusal stays loud in the other direction: a
    // program that never reads Data2 but is handed a second tape is a caller
    // contract violation, not silently ignored.
    let tape = load_csv_window(&sample_bars_path(), 0, 140);
    let data2 = make_data2_tape(&tape, 3.0, 0, 140);
    let source = "If Close > 0 Then Buy (\"PLAIN\") 1 Contracts Next Bar At Market;\n";
    let (ir, _) = python_reference(source, 0, 140);
    let error = run_program(
        &ir,
        &tape,
        Some(&data2),
        &nq_config(),
        &HashMap::<String, Value>::new(),
    )
    .expect_err("data2 supplied for a non-Data2 program must be refused");
    assert!(
        error.to_string().contains("does not reference Data2"),
        "unexpected refusal message: {error}"
    );
}

// ---- arrays and for loops ----

const ARRAYS_FIXTURE: &str = include_str!("fixtures/arrays_numeric.txt");
const LOOPS_FIXTURE: &str = include_str!("fixtures/loops_only.txt");
const ARRAYS_OOB_FIXTURE: &str = include_str!("fixtures/arrays_oob.txt");

/// Static and dynamic arrays (persisting across bars), negative-index wrap,
/// truncated fractional indexes, Array_SetMaxIndex growth and the native
/// Array_* functions, with the values in every fill's label.
#[test]
fn arrays_fixture_matches_python_on_full_tape_and_windows() {
    for (start, end) in [(0, 140), (0, 25), (37, 140)] {
        assert_python_native_parity(
            &format!("arrays fixture bars {start}..{end}"),
            ARRAYS_FIXTURE,
            start,
            end,
        );
    }
}

#[test]
fn for_loops_fixture_matches_python() {
    assert_python_native_parity("for-loop fixture", LOOPS_FIXTURE, 0, 140);
}

/// `Buf[n] = Close` with n past the last index raises IndexError in Python
/// (strict run fails); the native run must fail too, never clamp.
#[test]
fn out_of_range_array_index_fails_both_paths() {
    let script = "import sys\n\
        from pl_transpiler import tokenise\n\
        from pl_transpiler.ir import serialise_ir\n\
        from pl_transpiler.parser import parse\n\
        from pl_transpiler.runtime.instrument_config import get_config\n\
        from pl_transpiler.tools.pl_oracle import run_oracle\n\
        from pl_transpiler.tools.pl_run import load_bars_csv\n\
        source = sys.stdin.read()\n\
        tokens, positions = tokenise(source)\n\
        print(serialise_ir(parse(tokens, positions=positions, source=source)))\n\
        sys.stdout.flush()\n\
        run_oracle(source, load_bars_csv(sys.argv[1]), get_config('NQ'))\n";
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(sample_bars_path())
        .current_dir(repo_root())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn Python reference");
    child
        .stdin
        .take()
        .expect("Python stdin")
        .write_all(ARRAYS_OOB_FIXTURE.as_bytes())
        .expect("write EL source");
    let output = child.wait_with_output().expect("wait for Python reference");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !output.status.success() && stderr.contains("IndexError"),
        "the Python run must fail with IndexError:\n{stderr}"
    );
    let ir = String::from_utf8(output.stdout).expect("IR is UTF-8");
    let tape = load_csv_window(&sample_bars_path(), 0, 140);
    let error = run_program(
        ir.trim(),
        &tape,
        None,
        &nq_config(),
        &HashMap::<String, Value>::new(),
    )
    .expect_err("the native run must fail too");
    assert!(
        error
            .to_string()
            .contains("array index 6 out of range for buf"),
        "unexpected error: {error}"
    );
}

#[test]
fn session_time_words_match_python() {
    // `pl_sessionendtime` / `pl_sessionstarttime` ignore their arguments and
    // return the template constants. The sample tape runs 0930..1149.
    let source = "Variables: EndT(0), StartT(0);\n\
                  EndT = SessionEndTime(1, 1);\n\
                  StartT = SessionStartTime(0, 2) + Sess1EndTime - SessionEndTimeMS(1);\n\
                  If Time < EndT - 530 and StartT = 1700 Then\n\
                      Buy (\"SE\") 1 Contracts Next Bar At Market;\n\
                  If Time >= EndT - 500 Then\n\
                      Sell (\"SX\") Next Bar At Market;\n";
    assert_python_native_parity("SessionEndTime/SessionStartTime", source, 0, 140);
    assert_eq!(super::compile::session_time("sessionendtime"), Some(1600));
    assert_eq!(super::compile::session_time("sessionstarttime"), Some(1700));
    assert_eq!(super::compile::session_time("sessionlastbar"), None);
    // Bare SessionEndTime reads a run kwarg in Python, not the constant.
    assert_eq!(super::compile::bare_session_time("sessionendtime"), None);
    assert_eq!(
        super::compile::bare_session_time("sess1endtime"),
        Some(1515)
    );
}
