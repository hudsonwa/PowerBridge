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

//! PowerBridge native kernel extra (`pl_transpiler._accel`).
//!
//! The shared Rust-side types live in [`types`]. [`kernel`] is the native bar
//! loop and fill engine; [`strategy`] is the IR interpreter that drives it and
//! backs every `run_program*` entry point; [`audit`] builds the audit packet.
//! The stable [`run`] call still refuses execution because its seven-array
//! signature provides no EL program, AST, strategy identifier, instrument
//! configuration, or Data2. Hard-coding one strategy would silently return
//! wrong trades for every other program, so the crate deliberately chooses the
//! fail-loud fallback policy.

use std::collections::HashMap;

use pyo3::exceptions::{PyNotImplementedError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

/// Shared types (interface v2): bar/series shapes, the order/fill/position
/// surface, the output/report surface, the interpreter seam, and the explicit
/// config.
pub(crate) mod types;

// The native bar loop and fill engine every `run_program*` call runs on.
// Module-wide allow, narrowed to item level in the follow-up cleanup.
#[allow(dead_code)]
mod kernel;

mod audit;
mod strategy;

use types::{BarTape, Config, Fill, KernelError, Session};

/// Trivial export used to prove the FFI boundary. Always returns 42.
#[pyfunction]
fn ping() -> u32 {
    42
}

/// One-shot kernel: all bars in, trades out. A single FFI crossing per run.
///
/// Per-bar callbacks are intentionally not offered. Crossing the boundary once
/// per bar destroys the gain this extra exists to capture.
///
/// Parameters
/// ----------
/// open, high, low, close, volume : sequence of float, length N
///     Per-bar OHLC and volume. Same length as ``date`` and ``time``.
/// date, time : sequence of int, length N
///     EasyLanguage date (YYYYMMDD-offset form, e.g. 1180101) and time (HHmm).
///
/// Returns
/// -------
/// list[dict]
///     Fills in the ``FillEngine.fills`` shape used by ``pl_oracle``:
///     ``{"type", "signal", "date", "time", "price", "contracts"}``.
///
/// Raises
/// ------
/// NotImplementedError
///     The native bar loop and fill engine exist, but this stable call does not
///     carry a strategy program. The caller must loudly fall back to Python.
#[pyfunction]
#[pyo3(signature = (open, high, low, close, volume, date, time))]
fn run(
    open: Bound<'_, PyAny>,
    high: Bound<'_, PyAny>,
    low: Bound<'_, PyAny>,
    close: Bound<'_, PyAny>,
    volume: Bound<'_, PyAny>,
    date: Bound<'_, PyAny>,
    time: Bound<'_, PyAny>,
) -> PyResult<Vec<Py<PyAny>>> {
    // Marshal and validate the complete tape in this one call. There is no
    // Python callback (per-bar or otherwise).
    let _tape = BarTape::from_columns(
        open.extract::<Vec<f64>>()?,
        high.extract::<Vec<f64>>()?,
        low.extract::<Vec<f64>>()?,
        close.extract::<Vec<f64>>()?,
        volume.extract::<Vec<f64>>()?,
        date.extract::<Vec<i64>>()?,
        time.extract::<Vec<i64>>()?,
    )
    .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;

    // Selected strategy-semantics approach: fail-loud fallback.
    //
    // The stable signature has no EL/AST/strategy argument, and the reference strategy needs
    // aligned Data2 closes that are also absent. Running a no-op or hard-coded
    // strategy would be plausible-but-wrong. Raise a stable built-in exception
    // so the Python wiring owner can select the existing Python path explicitly.
    Err(PyNotImplementedError::new_err(
        "ACCEL_FALLBACK_REQUIRED: the native bar loop and FillEngine are built, \
         but run(open, high, low, close, volume, date, time) carries no EL program, \
         AST, strategy identifier, instrument config, or Data2; refusing to guess \
         strategy semantics -- run the existing Python path",
    ))
}

/// Execute a v2 `program_ir` over a `series` with an explicit `config`.
///
/// This is the strategy-bearing contract the stable [`run`] refuses to be: it
/// carries the serialised IR program, the aligned Data2 tape (if the program
/// references Data2), and the explicit instrument config, so a single FFI
/// crossing can produce the native trade list. Fidelity gate: the returned
/// fills must equal the Python path bit-for-bit, or the run raises — never a
/// partial/approximate trade list.
///
/// Parameters
/// ----------
/// program_ir : str
///     The `serialise_ir` payload (`{"ir_version": 1, "kind":
///     "pl_transpiler.ast", "ast": {...}}`).
/// series : dict
///     Mapped column bundle: ``{"open", "high", "low", "close", "volume"}``
///     sequences of float and ``{"date", "time"}`` sequences of int, all equal
///     length (the `BarTape` columnar layout).
/// data2 : dict | None
///     Optional aligned Data2 series in the same shape as ``series``. This
///     interpreter does not implement Data2 and refuses such programs (or a
///     supplied Data2) loudly.
/// config : dict
///     Explicit instrument config (no defaults): ``name``, ``bigpointvalue``,
///     ``pointvalue``, ``minmove``, ``pricescale``, ``tick``, ``barinterval``,
///     ``session`` (``{"start", "end"}``), ``commission``, ``slippage``,
///     ``max_bars_back``, ``max_entries_per_direction``, ``warmup_seed``,
///     ``symccy``, ``symnm``, ``symalt``, ``descstr``, ``expdate``.
///
/// Returns
/// -------
/// list[dict]
///     Ordered fills in the ``FillEngine.fills`` shape used by ``pl_oracle``:
///     ``{"type", "signal", "date", "time", "price", "contracts"}``.
///
/// Raises
/// ------
/// ValueError
///     Malformed caller input: missing/invalid config fields, a non-mapping
///     series, or a column-length mismatch.
/// NotImplementedError
///     A construct outside the implemented surface (e.g. Data2) — the caller
///     must fall back to the Python path loudly.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None))]
fn run_program(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<Py<PyAny>>> {
    let config = config
        .ok_or_else(|| PyValueError::new_err("accel run_program requires a 'config' dict"))?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let fills = run_program_core(program_ir, &tape, data2_tape.as_ref(), &config)
        .map_err(kernel_error_to_py)?;
    fills
        .into_iter()
        .map(|fill| fill_to_python(series.py(), fill))
        .collect()
}

/// `run_program` plus the drawing objects that run recorded.
///
/// Returns `(fills, objects_json)`. `objects_json` is a JSON array of
/// drawing-dump objects (`seq` left at 0; Python assigns creation order and
/// the envelope). Drawing calls this kernel does not implement are refused.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None))]
fn run_program_drawings(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
) -> PyResult<(Vec<Py<PyAny>>, String)> {
    let config = config.ok_or_else(|| {
        PyValueError::new_err("accel run_program_drawings requires a 'config' dict")
    })?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let inputs = HashMap::<String, strategy::Value>::new();
    let (fills, drawings) =
        strategy::run_program_drawings(program_ir, &tape, data2_tape.as_ref(), &config, &inputs)
            .map_err(kernel_error_to_py)?;
    let py_fills = fills
        .into_iter()
        .map(|fill| fill_to_python(series.py(), fill))
        .collect::<PyResult<_>>()?;
    Ok((py_fills, drawings))
}

/// The pure-Rust seam behind the [`run_program`] pyfunction: marshal-free, so
/// the FFI marshaling itself stays a thin layer and this core is unit-testable
/// without a Python interpreter.
fn run_program_core(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
) -> Result<Vec<Fill>, KernelError> {
    // No input overrides from the FFI surface: an empty map uses the IR Input
    // defaults (the stable `run_program` signature has no `inputs` argument).
    let inputs = HashMap::<String, strategy::Value>::new();
    run_variant_core(program_ir, series, data2, config, &inputs)
}

/// SPIKE seam: production loop + one framed `audit-event-v1` packet.
fn run_program_audit_packet_core(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    hash_tapes: bool,
) -> Result<(Vec<Fill>, Vec<u8>), KernelError> {
    let inputs = HashMap::<String, strategy::Value>::new();
    let (outcome, packet) =
        strategy::run_program_audit_packet(program_ir, series, data2, config, &inputs, hash_tapes)?;
    Ok((outcome.fills, packet))
}

/// The pure-Rust seam behind the [`run_program_inputs`] pyfunction: a single
/// variant with explicit input overrides (the sweep/batch "solo" reference).
fn run_variant_core(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    inputs: &HashMap<String, strategy::Value>,
) -> Result<Vec<Fill>, KernelError> {
    let outcome = strategy::run_program(program_ir, series, data2, config, inputs)?;
    Ok(outcome.fills)
}

/// The pure-Rust seam behind the [`run_program_batch`] pyfunction: `K` variants
/// over one already-marshalled tape, compiled once. `out[i]` is bit-identical to
/// `run_variant_core(..., variants[i])` — the sweep invariant the FFI batch
/// caller verifies against the solo path.
fn run_program_batch_core(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    variants: &[HashMap<String, strategy::Value>],
) -> Result<Vec<Vec<Fill>>, KernelError> {
    strategy::run_program_batch(program_ir, series, data2, config, variants)
}

/// The pure-Rust seam behind [`run_program_batch_parallel`]: the same sweep,
/// fanned across `threads` worker threads. Bit-identical to the sequential
/// batch — threads only read the shared tape/Data2, never mutate a shared answer.
fn run_program_batch_parallel_core(
    program_ir: &str,
    series: &BarTape,
    data2: Option<&BarTape>,
    config: &Config,
    variants: &[HashMap<String, strategy::Value>],
    threads: usize,
) -> Result<Vec<Vec<Fill>>, KernelError> {
    strategy::run_program_batch_parallel(program_ir, series, data2, config, variants, threads)
}

/// Map a native [`KernelError`] to the Python exception its class signals:
/// `input` → `ValueError`; `unsupported` / `strategy` → `NotImplementedError`
/// (the same "fall back loudly" signal as the stable [`run`] refusal).
fn kernel_error_to_py(error: KernelError) -> PyErr {
    let message = error.to_string();
    if message.starts_with("invalid accel input:") {
        PyValueError::new_err(message)
    } else {
        PyNotImplementedError::new_err(message)
    }
}

/// Extract the explicit [`Config`] from a Python mapping. Every field is
/// required: `Config` has no `Default` (v2 requirement 2-3) and a missing
/// field raises rather than being invented.
fn extract_config(config: &Bound<'_, PyDict>) -> PyResult<Config> {
    fn required<'py>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
        dict.get_item(key)?
            .ok_or_else(|| PyValueError::new_err(format!("accel config is missing {key:?}")))
    }
    fn cfg_f64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
        required(dict, key)?
            .extract::<f64>()
            .map_err(|_| PyTypeError::new_err(format!("accel config {key:?} must be a number")))
    }
    fn cfg_int(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<i64> {
        required(dict, key)?
            .extract::<i64>()
            .map_err(|_| PyTypeError::new_err(format!("accel config {key:?} must be an integer")))
    }
    fn cfg_usize(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<usize> {
        required(dict, key)?
            .extract::<usize>()
            .map_err(|_| PyTypeError::new_err(format!("accel config {key:?} must be an integer")))
    }
    fn cfg_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
        required(dict, key)?
            .extract::<String>()
            .map_err(|_| PyTypeError::new_err(format!("accel config {key:?} must be a string")))
    }

    let session = required(config, "session")?
        .downcast_into::<PyDict>()
        .map_err(|_| PyTypeError::new_err("accel config 'session' must be a dict"))?;
    let session_start = cfg_int(&session, "start")?;
    let session_end = cfg_int(&session, "end")?;

    Ok(Config {
        name: cfg_str(config, "name")?,
        bigpointvalue: cfg_f64(config, "bigpointvalue")?,
        pointvalue: cfg_f64(config, "pointvalue")?,
        minmove: cfg_int(config, "minmove")?,
        pricescale: cfg_int(config, "pricescale")?,
        tick: cfg_f64(config, "tick")?,
        barinterval: cfg_int(config, "barinterval")?,
        session: Session {
            start: session_start,
            end: session_end,
        },
        commission: cfg_f64(config, "commission")?,
        slippage: cfg_f64(config, "slippage")?,
        max_bars_back: cfg_usize(config, "max_bars_back")?,
        max_entries_per_direction: cfg_usize(config, "max_entries_per_direction")?,
        warmup_seed: cfg_usize(config, "warmup_seed")?,
        symccy: cfg_int(config, "symccy")?,
        symnm: cfg_str(config, "symnm")?,
        symalt: cfg_str(config, "symalt")?,
        descstr: cfg_str(config, "descstr")?,
        expdate: cfg_int(config, "expdate")?,
    })
}

/// Extract a [`BarTape`] from a Python mapping of the seven column sequences.
fn extract_tape(series: &Bound<'_, PyAny>) -> PyResult<BarTape> {
    let columns: &Bound<'_, PyDict> = series.downcast::<PyDict>().map_err(|_| {
        PyValueError::new_err(
            "accel series must be a dict with keys open, high, low, close, volume, date, time",
        )
    })?;
    fn col_f64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<f64>> {
        dict.get_item(key)?
            .ok_or_else(|| {
                PyValueError::new_err(format!("accel series is missing column {key:?}"))
            })?
            .extract::<Vec<f64>>()
            .map_err(|_| {
                PyTypeError::new_err(format!(
                    "accel series column {key:?} must be a sequence of numbers"
                ))
            })
    }
    fn col_int(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<i64>> {
        dict.get_item(key)?
            .ok_or_else(|| {
                PyValueError::new_err(format!("accel series is missing column {key:?}"))
            })?
            .extract::<Vec<i64>>()
            .map_err(|_| {
                PyTypeError::new_err(format!(
                    "accel series column {key:?} must be a sequence of integers"
                ))
            })
    }
    BarTape::from_columns(
        col_f64(columns, "open")?,
        col_f64(columns, "high")?,
        col_f64(columns, "low")?,
        col_f64(columns, "close")?,
        col_f64(columns, "volume")?,
        col_int(columns, "date")?,
        col_int(columns, "time")?,
    )
    .map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Convert a native fill to the stable Python dictionary shape.
///
/// Kept next to the FFI boundary so a future strategy-bearing contract can
/// return kernel fills without changing their keys or value types.
fn fill_to_python(py: Python<'_>, fill: Fill) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("type", fill.fill_type)?;
    dict.set_item("signal", fill.signal)?;
    dict.set_item("date", fill.date)?;
    dict.set_item("time", fill.time)?;
    dict.set_item("price", fill.price)?;
    dict.set_item("contracts", fill.contracts)?;
    Ok(dict.into_any().unbind())
}

/// Convert a typed trace cell to its Python object (float / int / bool / str).
fn cell_to_python(py: Python<'_>, cell: strategy::Cell) -> PyResult<Py<PyAny>> {
    use pyo3::IntoPyObjectExt;
    Ok(match cell {
        strategy::Cell::Num(n) => n.into_py_any(py)?,
        strategy::Cell::Int(i) => i.into_py_any(py)?,
        strategy::Cell::Bool(b) => b.into_py_any(py)?,
        strategy::Cell::Str(s) => s.into_py_any(py)?,
    })
}

/// Execute a v2 `program_ir` natively and return the typed per-bar trace plus
/// the ordered fills the same loop produced.
///
/// This is the typed trace bridge: one FFI crossing emits the
/// kernel's register values **typed** (not pre-formatted strings), so Python
/// owns text rendering. It reproduces the `run_el` full-trace shape but typed,
/// and is independent of the `audit-event-v1` packet (the measured sub-second
/// path). `columns` is the sorted union of bar metadata, engine position
/// columns and the strategy's variables and inputs; `rows` is one typed row
/// per bar aligned to `columns`. `str()` of every cell is the Python trace's
/// cell: each name's int/float/bool/str type is fixed at compile time, and a
/// program whose types depend on the run is refused.
///
/// Returns ``(columns, rows, fills)``. ``fills`` is the same ordered
/// ``FillEngine.fills`` list [`run_program`] returns, so the trade-list digest
/// of a trace run must equal `pl_oracle` bit-for-bit.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None))]
#[allow(clippy::type_complexity)]
fn run_program_trace(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
) -> PyResult<(Vec<String>, Vec<Vec<Py<PyAny>>>, Vec<Py<PyAny>>)> {
    let config = config
        .ok_or_else(|| PyValueError::new_err("accel run_program_trace requires a 'config' dict"))?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let (trace, fills) =
        strategy::run_program_trace(program_ir, &tape, data2_tape.as_ref(), &config)
            .map_err(kernel_error_to_py)?;
    let py = series.py();
    let rows: Vec<Vec<Py<PyAny>>> = trace
        .rows
        .into_iter()
        .map(|row| {
            row.into_iter()
                .map(|cell| cell_to_python(py, cell))
                .collect::<PyResult<Vec<Py<PyAny>>>>()
        })
        .collect::<PyResult<Vec<Vec<Py<PyAny>>>>>()?;
    let fills: Vec<Py<PyAny>> = fills
        .into_iter()
        .map(|fill| fill_to_python(py, fill))
        .collect::<PyResult<_>>()?;
    Ok((trace.columns, rows, fills))
}

/// Every construct the native compiler would refuse in `program_ir`.
///
/// Returns a list of ``(kind, name, line, message)`` tuples in compile order;
/// an empty list means the compile pass accepts the program. ``message`` of
/// the first entry is the ``NotImplementedError`` text [`run_program`] raises.
/// Malformed IR raises ``ValueError``.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn explain_program(program_ir: &str) -> PyResult<Vec<(&'static str, String, i64, String)>> {
    strategy::explain_program(program_ir).map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Convert one Python value to a strategy input [`strategy::Value`] (number,
/// boolean, or string — the three kinds the IR input surface admits).
fn extract_input_value(value: &Bound<'_, PyAny>) -> PyResult<strategy::Value> {
    if let Ok(b) = value.extract::<bool>() {
        return Ok(strategy::Value::Bool(b));
    }
    if let Ok(n) = value.extract::<f64>() {
        return Ok(strategy::Value::Num(n));
    }
    let s: String = value.extract()?;
    Ok(strategy::Value::str(s))
}

/// Convert a Python input-override mapping (name -> value) into the Rust
/// `HashMap<String, Value>` the strategy layer consumes.
fn extract_inputs(variant: &Bound<'_, PyDict>) -> PyResult<HashMap<String, strategy::Value>> {
    let mut map = HashMap::with_capacity(variant.len());
    for (key, value) in variant.iter() {
        let name: String = key.extract()?;
        map.insert(name, extract_input_value(&value)?);
    }
    Ok(map)
}

/// Convert the Python `variants` list (a list of input-override dicts) into the
/// Rust slice of `HashMap<String, Value>` the strategy layer consumes.
fn extract_variants(
    variants: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<HashMap<String, strategy::Value>>> {
    let variants_list =
        variants.ok_or_else(|| PyValueError::new_err("accel sweep requires a 'variants' list"))?;
    let variants_list = variants_list
        .downcast::<PyList>()
        .map_err(|_| PyTypeError::new_err("accel 'variants' must be a list"))?;
    let mut parsed = Vec::with_capacity(variants_list.len());
    for item in variants_list.iter() {
        let dict = item
            .downcast::<PyDict>()
            .map_err(|_| PyTypeError::new_err("accel each variant must be a dict"))?;
        parsed.push(extract_inputs(dict)?);
    }
    Ok(parsed)
}

/// Execute a v2 `program_ir` over a `series` with explicit `config` AND explicit
/// input overrides. This is the sweep/batch "solo" reference: one variant run
/// in isolation, against which the batch path must be bit-identical.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None, inputs=None))]
fn run_program_inputs(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
    inputs: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<Py<PyAny>>> {
    let config = config.ok_or_else(|| {
        PyValueError::new_err("accel run_program_inputs requires a 'config' dict")
    })?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let inputs_map = match inputs {
        Some(dict) => extract_inputs(dict)?,
        None => HashMap::new(),
    };
    let fills = run_variant_core(program_ir, &tape, data2_tape.as_ref(), &config, &inputs_map)
        .map_err(kernel_error_to_py)?;
    fills
        .into_iter()
        .map(|fill| fill_to_python(series.py(), fill))
        .collect()
}

/// Execute a sweep: `variants` (a list of input-override dicts) over ONE tape,
/// compiled once. Returns a list of per-variant fill lists; `out[i]` is
/// bit-identical to a solo `run_program_inputs` with `variants[i]`.
///
/// This is the batch/prepared path the 1000x sweep architecture needs: the tape
/// is marshalled once, the program parsed+lowered once, and only the per-variant
/// state (input vector, variables, history, fill engine) is rebuilt K times.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None, variants=None))]
fn run_program_batch(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
    variants: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<Py<PyAny>>> {
    let config = config
        .ok_or_else(|| PyValueError::new_err("accel run_program_batch requires a 'config' dict"))?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let parsed = extract_variants(variants)?;
    let fills = run_program_batch_core(program_ir, &tape, data2_tape.as_ref(), &config, &parsed)
        .map_err(kernel_error_to_py)?;
    let py = series.py();
    fills
        .into_iter()
        .map(|variant_fills| {
            let list = PyList::new(py, variant_fills.iter().map(|_| py.None()))?;
            for (i, fill) in variant_fills.into_iter().enumerate() {
                list.set_item(i, fill_to_python(py, fill)?)?;
            }
            Ok(list.into_any().unbind())
        })
        .collect()
}

/// Execute a sweep across multiple worker threads (the released-GIL
/// embarrassingly-parallel path). `threads` bounds the worker count; `None`
/// uses all available parallelism. Identical output to [`run_program_batch`],
/// bit-for-bit.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None, variants=None, threads=None))]
fn run_program_batch_parallel(
    py: Python<'_>,
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
    variants: Option<&Bound<'_, PyAny>>,
    threads: Option<usize>,
) -> PyResult<Vec<Py<PyAny>>> {
    let config = extract_config(config.ok_or_else(|| {
        PyValueError::new_err("accel run_program_batch_parallel requires a 'config' dict")
    })?)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let parsed = extract_variants(variants)?;
    let nthreads = threads.unwrap_or_else(|| {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1)
    });
    // Release the GIL: the sweep loop below is pure Rust (no Python objects),
    // so K worker threads can run the bar loop concurrently across cores.
    let fills = py.allow_threads(|| {
        run_program_batch_parallel_core(
            program_ir,
            &tape,
            data2_tape.as_ref(),
            &config,
            &parsed,
            nthreads,
        )
        .map_err(kernel_error_to_py)
    })?;
    let py = series.py();
    fills
        .into_iter()
        .map(|variant_fills| {
            let list = PyList::new(py, variant_fills.iter().map(|_| py.None()))?;
            for (i, fill) in variant_fills.into_iter().enumerate() {
                list.set_item(i, fill_to_python(py, fill)?)?;
            }
            Ok(list.into_any().unbind())
        })
        .collect()
}

/// SPIKE: production `run_program` loop returning one framed `audit-event-v1`
/// packet (`bytes`). Does not change the stable `run_program` fill list.
///
/// `hash_tapes` (default false) SHA-256s the in-memory Data1 columns into the
/// header. Data2 hashing is reserved (zeros). Tape hashing is off the timed
/// collection path.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None, hash_tapes=false))]
fn run_program_audit_packet(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
    hash_tapes: bool,
) -> PyResult<Py<PyBytes>> {
    let config = config.ok_or_else(|| {
        PyValueError::new_err("accel run_program_audit_packet requires a 'config' dict")
    })?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let (_fills, packet) =
        run_program_audit_packet_core(program_ir, &tape, data2_tape.as_ref(), &config, hash_tapes)
            .map_err(kernel_error_to_py)?;
    Ok(PyBytes::new(series.py(), &packet).unbind())
}

/// Prove the prefix skip (`eval_from`) from the IR + Data1 date column alone.
///
/// Returns ``(eval_from, proven, reason)``. A slice-before-marshal caller uses
/// this to learn the slice point WITHOUT first marshalling the seven OHLC
/// columns across the FFI. ``dates`` must be the full Data1 date column
/// (int sequence); OHLC is not read by the skip proof.
#[pyfunction]
#[pyo3(signature = (program_ir, dates, inputs=None))]
fn audit_skip_eval_from(
    program_ir: &str,
    dates: &Bound<'_, PyAny>,
    inputs: Option<&Bound<'_, PyDict>>,
) -> PyResult<(usize, bool, String)> {
    let dates: Vec<i64> = dates.extract().map_err(|_| {
        PyTypeError::new_err("accel audit_skip_eval_from 'dates' must be a sequence of integers")
    })?;
    let inputs_map = match inputs {
        Some(dict) => extract_inputs(dict)?,
        None => HashMap::new(),
    };
    let plan =
        strategy::skip_eval_from(program_ir, &dates, &inputs_map).map_err(kernel_error_to_py)?;
    Ok((plan.eval_from, plan.proven, plan.reason))
}

/// One `audit-event-v1` packet from an already-sliced `series` (the slice-
/// before-marshal solo reference). `eval_from` is the absolute slice offset and
/// `source_bar_count` the full tape length; both feed the packet header so the
/// result is byte-identical to [`run_program_audit_packet`] over the full tape.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None, inputs=None, eval_from=0, source_bar_count=None))]
fn run_program_audit_packet_inputs(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
    inputs: Option<&Bound<'_, PyDict>>,
    eval_from: usize,
    source_bar_count: Option<usize>,
) -> PyResult<Py<PyBytes>> {
    let config = config.ok_or_else(|| {
        PyValueError::new_err("accel run_program_audit_packet_inputs requires a 'config' dict")
    })?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let inputs_map = match inputs {
        Some(dict) => extract_inputs(dict)?,
        None => HashMap::new(),
    };
    let source_bar_count = source_bar_count.unwrap_or_else(|| tape.len());
    let packet = strategy::run_program_audit_packet_sliced(
        program_ir,
        &tape,
        data2_tape.as_ref(),
        &config,
        &inputs_map,
        eval_from,
        source_bar_count,
    )
    .map_err(kernel_error_to_py)?;
    Ok(PyBytes::new(series.py(), &packet).unbind())
}

/// A sweep of `audit-event-v1` packets (`variants` input overrides) over ONE
/// already-sliced `series`, compiled and marshalled once. `out[i]` is
/// byte-identical to [`run_program_audit_packet_inputs`] with `variants[i]`.
#[pyfunction]
#[pyo3(signature = (program_ir, series, data2=None, config=None, variants=None, eval_from=0, source_bar_count=None))]
fn run_program_audit_packet_batch(
    program_ir: &str,
    series: &Bound<'_, PyAny>,
    data2: Option<&Bound<'_, PyAny>>,
    config: Option<&Bound<'_, PyDict>>,
    variants: Option<&Bound<'_, PyAny>>,
    eval_from: usize,
    source_bar_count: Option<usize>,
) -> PyResult<Vec<Py<PyAny>>> {
    let config = config.ok_or_else(|| {
        PyValueError::new_err("accel run_program_audit_packet_batch requires a 'config' dict")
    })?;
    let config = extract_config(config)?;
    let tape = extract_tape(series)?;
    let data2_tape = match data2 {
        Some(d2) => Some(extract_tape(d2)?),
        None => None,
    };
    let parsed = extract_variants(variants)?;
    let source_bar_count = source_bar_count.unwrap_or_else(|| tape.len());
    let packets = strategy::run_program_audit_packet_batch(
        program_ir,
        &tape,
        data2_tape.as_ref(),
        &config,
        &parsed,
        eval_from,
        source_bar_count,
    )
    .map_err(kernel_error_to_py)?;
    let py = series.py();
    packets
        .into_iter()
        .map(|packet| Ok(PyBytes::new(py, &packet).unbind().into_any()))
        .collect()
}

/// Fill-log semantics this kernel implements. The core refuses the kernel
/// (and runs the Python path) unless this equals its own `FILL_LOG_VERSION`.
pub const FILL_LOG_VERSION: u32 = 2;

/// Native extension module. Import as ``pl_transpiler._accel``.
#[pymodule]
fn _accel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("FILL_LOG_VERSION", FILL_LOG_VERSION)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    // Capability flag: `run_program` honours `config["max_bars_back"]`. A
    // kernel without it ignores the field, so Python falls back for N > 0.
    m.add("MAX_BARS_BACK", true)?;
    // Capability flag: `run_program` honours `config["max_entries_per_direction"]`.
    m.add("MAX_ENTRIES_PER_DIRECTION", true)?;
    // Capability flag: `run_program_trace` honours both options above too.
    m.add("TRACE_RUN_OPTIONS", true)?;
    // Array_Sum must add like this interpreter's `sum()` (compensated from 3.12,
    // ints too from 3.14), read at import from the running interpreter.
    let version = m.py().version_info();
    strategy::set_python_sum(strategy::PySum::for_version(version.major, version.minor));
    m.add_function(wrap_pyfunction!(ping, m)?)?;
    m.add_function(wrap_pyfunction!(run, m)?)?;
    m.add_function(wrap_pyfunction!(run_program, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_drawings, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_inputs, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_batch, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_batch_parallel, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_trace, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_audit_packet, m)?)?;
    m.add_function(wrap_pyfunction!(audit_skip_eval_from, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_audit_packet_inputs, m)?)?;
    m.add_function(wrap_pyfunction!(run_program_audit_packet_batch, m)?)?;
    m.add_function(wrap_pyfunction!(explain_program, m)?)?;
    Ok(())
}

#[cfg(test)]
mod ffi_tests;
