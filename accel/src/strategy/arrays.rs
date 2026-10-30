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

//! EL arrays, copying the Python runtime's list semantics exactly.
//!
//! The Python codegen keeps an EL array as a list in *reverse* order: `A[i]`
//! is `A[-(int(i) + 1)]` (`codegen_expr._gen_bar_ref`). This module keeps the
//! elements in EL order and reproduces what that reversed indexing does,
//! including the parts a "correct" EL array would not: a fractional index
//! truncates toward zero. A negative index, or one at or past the end, fails
//! the run (the codegen's `el_array_index` stops the reversed list wrapping).
//!
//! The `Array_*` functions are ports of `_rt_arrays_dates.py` /
//! `_rt_builtins_az.py` / `_rt_datetime.py`; each method names its source.

use std::fmt;
use std::sync::atomic::{AtomicU8, Ordering};

use crate::types::KernelError;

use super::compile::{ArrayDecl, ArrayOp, ElemKind, MAX_ARRAY_INDEX};
use super::value::Value;

/// How the running Python's `sum()` adds numbers once its total is a float.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PySum {
    /// Before 3.12: every element is added plainly.
    Plain,
    /// 3.12 and 3.13: floats use Neumaier compensation, ints are added plainly.
    CompensatedFloats,
    /// 3.14+: ints are converted to float and compensated like floats.
    CompensatedAll,
}

impl PySum {
    /// The `sum()` flavour of Python `major.minor`.
    pub(crate) fn for_version(major: u8, minor: u8) -> Self {
        match (major, minor) {
            (3, ..=11) => Self::Plain,
            (3, 12 | 13) => Self::CompensatedFloats,
            _ => Self::CompensatedAll,
        }
    }
}

/// The importing interpreter's `PySum`, stored as its discriminant.
static PY_SUM: AtomicU8 = AtomicU8::new(PySum::Plain as u8);

/// Record how the calling Python's `sum()` adds (set once at import).
pub(crate) fn set_python_sum(mode: PySum) {
    PY_SUM.store(mode as u8, Ordering::Relaxed);
}

pub(crate) fn python_sum() -> PySum {
    match PY_SUM.load(Ordering::Relaxed) {
        0 => PySum::Plain,
        1 => PySum::CompensatedFloats,
        _ => PySum::CompensatedAll,
    }
}

/// Elements `pl_array_copy` writes into the destination, starting at `at`.
#[derive(Debug, Clone)]
pub(crate) struct CopyChunk {
    at: usize,
    items: Vec<Value>,
    kinds: Vec<ElemKind>,
}

/// One array's elements in EL index order (`A[0]` first), with the Python
/// type of each element as far as the compiler knew it.
#[derive(Debug, Clone)]
pub(crate) struct ElArray {
    name: String,
    items: Vec<Value>,
    kinds: Vec<ElemKind>,
}

/// Python `int(x)` for a value the kernel holds (truncates toward zero).
/// `what` names the index in errors; it is only formatted on failure, so
/// hot callers pass `format_args!` instead of building a `String`.
pub(crate) fn py_index_int(value: &Value, what: impl fmt::Display) -> Result<i64, KernelError> {
    match value {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Num(n) if n.is_nan() => Err(KernelError::strategy(format!(
            "{what}: cannot convert float NaN to integer"
        ))),
        Value::Num(n) if n.is_infinite() => Err(KernelError::strategy(format!(
            "{what}: cannot convert float infinity to integer"
        ))),
        // 2^63: beyond it Python has a big int no list index can use.
        Value::Num(n) if n.trunc().abs() >= 9_223_372_036_854_775_808.0 => Err(
            KernelError::strategy(format!("{what}: {n} does not fit an index")),
        ),
        Value::Num(n) => Ok(n.trunc() as i64),
        Value::NonNum(_) => Err(KernelError::unsupported(format!("{what} is not a number"))),
    }
}

/// A value Python uses directly as an `int` (a slice bound or a repeat
/// count). The compiler only lets int-typed expressions through; a
/// non-integral value here (an input overridden with a fraction) is the
/// `TypeError` Python would raise.
fn py_index(value: &Value, what: &str) -> Result<i64, KernelError> {
    match value {
        Value::Num(n) if n.fract() != 0.0 || !n.is_finite() => Err(KernelError::strategy(format!(
            "{what}: slice indices must be integers, got {n}"
        ))),
        _ => py_index_int(value, what),
    }
}

/// Element position of EL index `i`: `None` unless `0 <= i < len`.
fn element_pos(len: usize, i: i64) -> Option<usize> {
    usize::try_from(i).ok().filter(|&pos| pos < len)
}

/// Python slice `items[start:stop]` bounds (clamped, negatives from the end).
fn py_slice(len: usize, start: i64, stop: i64) -> (usize, usize) {
    let n = len as i64;
    let norm = |x: i64| {
        if x < 0 {
            (x.saturating_add(n)).max(0)
        } else {
            x.min(n)
        }
    };
    let (lo, hi) = (norm(start), norm(stop));
    (lo as usize, hi.max(lo) as usize)
}

/// Python `==` between two elements (`0 == 0.0 == False`; a `str` equals
/// only an equal `str`).
fn py_eq(a: &Value, b: &Value) -> bool {
    match (a.as_str(), b.as_str()) {
        (Some(x), Some(y)) => x == y,
        (None, None) => match (a.as_f64(), b.as_f64()) {
            (Ok(x), Ok(y)) => x == y,
            _ => false,
        },
        _ => false,
    }
}

fn num(value: &Value, what: &str) -> Result<f64, KernelError> {
    match value {
        Value::Num(n) => Ok(*n),
        Value::Bool(b) => Ok(f64::from(u8::from(*b))),
        Value::NonNum(_) => Err(KernelError::strategy(format!(
            "{what}: unsupported operand type"
        ))),
    }
}

/// Python `sum(items)` with its start value `0`.
fn py_sum(
    items: &[Value],
    kinds: &[ElemKind],
    name: &str,
    mode: PySum,
) -> Result<f64, KernelError> {
    let what = "Array_Sum";
    py_sum_by(
        items.len(),
        |i| num(&items[i], what),
        |i| kinds[i],
        what,
        name,
        mode,
    )
}

/// Python `sum()` with its start value `0` over `len` numbers, the `i`th
/// read by `value_at` with the Python type `kind_at` (`name` is the summed
/// thing, for errors).
///
/// Before 3.12 every element is added in order. From 3.12 (CPython
/// `builtin_sum_impl`) leading ints are summed exactly, the first float is
/// added plainly, and later floats use Neumaier compensation; the
/// compensation is applied at the end when finite. Later ints are added
/// plainly in 3.12 and 3.13 (or, past a C long, end the float fast path,
/// which is refused here) and compensated like floats from 3.14.
pub(crate) fn py_sum_by(
    len: usize,
    value_at: impl Fn(usize) -> Result<f64, KernelError>,
    kind_at: impl Fn(usize) -> ElemKind,
    what: &str,
    name: &str,
    mode: PySum,
) -> Result<f64, KernelError> {
    if mode == PySum::Plain {
        let mut total = 0.0;
        for i in 0..len {
            total += value_at(i)?;
        }
        return Ok(total);
    }
    if (0..len).any(|i| !matches!(kind_at(i), ElemKind::Int | ElemKind::Float)) {
        return Err(KernelError::unsupported(format!(
            "{what} over {name}: an element's Python int/float type is not known \
             natively, and Python 3.12+ sums floats with compensation"
        )));
    }
    let mut k = 0;
    let mut f = 0.0;
    while k < len && kind_at(k) == ElemKind::Int {
        f += value_at(k)?;
        k += 1;
    }
    if k == len {
        return Ok(f);
    }
    f += value_at(k)?;
    let mut c = 0.0f64;
    for i in k + 1..len {
        let x = value_at(i)?;
        if kind_at(i) == ElemKind::Int && mode == PySum::CompensatedFloats {
            // CPython 3.12/3.13 add an int that fits a C long plainly.
            if x.is_nan() || x.abs() >= 9_223_372_036_854_775_808.0 {
                return Err(KernelError::unsupported(format!(
                    "{what} over {name}: an int past the C long range ends \
                     Python's float fast path"
                )));
            }
            f += x;
        } else {
            let t = f + x;
            if f.abs() >= x.abs() {
                c += (f - t) + x;
            } else {
                c += (x - t) + f;
            }
            f = t;
        }
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    Ok(f)
}

impl ElArray {
    pub(crate) fn new(decl: &ArrayDecl) -> Self {
        Self {
            name: decl.name.clone(),
            items: vec![decl.init.clone(); decl.len],
            kinds: vec![decl.init_kind; decl.len],
        }
    }

    pub(crate) fn len(&self) -> usize {
        self.items.len()
    }

    #[cfg(test)]
    pub(crate) fn items(&self) -> &[Value] {
        &self.items
    }

    fn index_error(&self, i: i64) -> KernelError {
        KernelError::strategy(format!(
            "array index {i} out of range for {} (max index {})",
            self.name,
            self.items.len() as i64 - 1
        ))
    }

    /// `A[i]` (`A[-(el_array_index(i) + 1)]`), with the element's type.
    pub(crate) fn get(&self, index: &Value) -> Result<(Value, ElemKind), KernelError> {
        let i = py_index_int(index, format_args!("index of {}", self.name))?;
        let pos = element_pos(self.items.len(), i).ok_or_else(|| self.index_error(i))?;
        Ok((self.items[pos].clone(), self.kinds[pos]))
    }

    /// `A[i] = value`. The value is stored as is (no f32 narrowing).
    pub(crate) fn set(
        &mut self,
        index: &Value,
        value: Value,
        kind: ElemKind,
    ) -> Result<(), KernelError> {
        if value.is_str() {
            return Err(KernelError::unsupported(format!(
                "string element written into array {}",
                self.name
            )));
        }
        let i = py_index_int(index, format_args!("index of {}", self.name))?;
        let pos = element_pos(self.items.len(), i).ok_or_else(|| self.index_error(i))?;
        self.items[pos] = value;
        self.kinds[pos] = kind;
        Ok(())
    }

    /// `el_array_pos`: `int(i)` must be in `0..len` (no wrap). `None` is
    /// the `IndexError` / `ValueError` the Get/Set*Value callers swallow; an
    /// infinite index raises `OverflowError`, which they do not.
    fn checked_pos(&self, index: &Value) -> Result<Option<usize>, KernelError> {
        match index {
            Value::Num(n) if n.is_nan() => return Ok(None),
            Value::NonNum(_) => {
                return Err(KernelError::unsupported(format!(
                    "Array_*Value index into {} is not a number",
                    self.name
                )))
            }
            _ => {}
        }
        let i = py_index_int(index, format_args!("Array_*Value index into {}", self.name))?;
        Ok(usize::try_from(i).ok().filter(|i| *i < self.items.len()))
    }

    fn check_storable(&self, value: &Value) -> Result<(), KernelError> {
        if value.is_str() {
            return Err(KernelError::unsupported(format!(
                "string element written into array {}",
                self.name
            )));
        }
        Ok(())
    }

    /// A whole-array function; `args` are the EL arguments after the array.
    pub(crate) fn call(
        &mut self,
        op: ArrayOp,
        args: &[Value],
        kind: ElemKind,
    ) -> Result<Value, KernelError> {
        match op {
            // pl_array_setmaxindex: grow (never shrink) by prepending int 0s
            // to the Python list, i.e. new high EL indexes; returns True.
            ArrayOp::SetMaxIndex => {
                let len = self.items.len() as i64;
                let wanted = match &args[0] {
                    // `len(arr) <= nan` is False: nothing to do.
                    Value::Num(n) if n.is_nan() => return Ok(Value::Bool(true)),
                    Value::Num(n) if *n < len as f64 => return Ok(Value::Bool(true)),
                    other => py_index(other, "Array_SetMaxIndex")?,
                };
                if wanted >= len {
                    if wanted > MAX_ARRAY_INDEX {
                        return Err(KernelError::unsupported(format!(
                            "Array_SetMaxIndex({}, {wanted}) is above the native limit \
                             {MAX_ARRAY_INDEX}",
                            self.name
                        )));
                    }
                    let new_len = wanted as usize + 1;
                    self.items.resize(new_len, Value::Num(0.0));
                    self.kinds.resize(new_len, ElemKind::Int);
                }
                Ok(Value::Bool(true))
            }
            // pl_array_getmaxindex: len(arr) - 1.
            ArrayOp::GetMaxIndex => Ok(Value::Num(self.items.len() as f64 - 1.0)),
            // pl_array_sum: sum(el_array_items(arr)[start:end + 1]).
            ArrayOp::Sum => {
                let start = py_index(&args[0], "Array_Sum")?;
                let end = py_index(&args[1], "Array_Sum")?;
                let (lo, hi) = py_slice(self.items.len(), start, end.saturating_add(1));
                py_sum(
                    &self.items[lo..hi],
                    &self.kinds[lo..hi],
                    &self.name,
                    python_sum(),
                )
                .map(Value::Num)
            }
            // pl_array_highest / pl_array_lowest:
            // max(items[start:end + 1]) if start <= end < len(arr) else 0.0.
            ArrayOp::Highest | ArrayOp::Lowest => {
                let highest = op == ArrayOp::Highest;
                let what = if highest {
                    "Array_Highest"
                } else {
                    "Array_Lowest"
                };
                let (start, end) = (num(&args[0], what)?, num(&args[1], what)?);
                if !(start <= end && end < self.items.len() as f64) {
                    return Ok(Value::Num(0.0));
                }
                let (lo, hi) = py_slice(
                    self.items.len(),
                    py_index(&args[0], what)?,
                    py_index(&args[1], what)?.saturating_add(1),
                );
                let window = &self.items[lo..hi];
                let mut best = window.first().ok_or_else(|| {
                    KernelError::strategy(format!("{what}: arg is an empty sequence"))
                })?;
                let mut best_num = num(best, what)?;
                // max()/min() keep the first extreme: replace only on a
                // strict `>` / `<`.
                for item in &window[1..] {
                    let x = num(item, what)?;
                    if (highest && x > best_num) || (!highest && x < best_num) {
                        best = item;
                        best_num = x;
                    }
                }
                Ok(best.clone())
            }
            // pl_fill_array: every element = value; returns 0.
            ArrayOp::Fill => {
                self.check_storable(&args[0])?;
                self.items.fill(args[0].clone());
                self.kinds.fill(kind);
                Ok(Value::Num(0.0))
            }
            // pl_array_setvalrange: Arr[max(int(b), 0) .. min(int(e), len - 1)]
            // = value; returns 0.
            ArrayOp::SetValRange => {
                let lo = py_index_int(&args[0], "Array_SetValRange")?.max(0);
                let hi =
                    py_index_int(&args[1], "Array_SetValRange")?.min(self.items.len() as i64 - 1);
                if lo <= hi {
                    self.check_storable(&args[2])?;
                    let (lo, hi) = (lo as usize, hi as usize);
                    self.items[lo..=hi].fill(args[2].clone());
                    self.kinds[lo..=hi].fill(kind);
                }
                Ok(Value::Num(0.0))
            }
            // pl_array_indexof: el_array_items(arr).index(value), else -1.
            ArrayOp::IndexOf => Ok(Value::Num(
                self.items
                    .iter()
                    .position(|item| py_eq(item, &args[0]))
                    .map_or(-1.0, |i| i as f64),
            )),
            // pl_array_contains: value in arr.
            ArrayOp::Contains => Ok(Value::Bool(
                self.items.iter().any(|item| py_eq(item, &args[0])),
            )),
            // _pl_array_getval: arr[el_array_pos(arr, i)], else the default.
            ArrayOp::GetValue { bool_default } => match self.checked_pos(&args[0])? {
                Some(pos) => Ok(self.items[pos].clone()),
                None if bool_default => Ok(Value::Bool(false)),
                None => Ok(Value::Num(0.0)),
            },
            // _pl_array_setval: write and return the value, else the default.
            ArrayOp::SetValue { bool_default } => match self.checked_pos(&args[0])? {
                Some(pos) => {
                    self.check_storable(&args[1])?;
                    self.items[pos] = args[1].clone();
                    self.kinds[pos] = kind;
                    Ok(args[1].clone())
                }
                None if bool_default => Ok(Value::Bool(false)),
                None => Ok(Value::Num(0.0)),
            },
        }
    }

    /// The chunk `pl_array_copy` writes: `el_array_items(src)[si:si + n]`
    /// clipped to `max(len(dst) - di, 0)`, or `None` when it returns early.
    pub(crate) fn copy_chunk(
        &self,
        dst_len: usize,
        si: &Value,
        di: &Value,
        n: &Value,
    ) -> Result<Option<CopyChunk>, KernelError> {
        let si = py_index_int(si, "Array_Copy")?;
        let di = py_index_int(di, "Array_Copy")?;
        let n = py_index_int(n, "Array_Copy")?;
        if si < 0 || di < 0 || n <= 0 {
            return Ok(None);
        }
        let (lo, hi) = py_slice(self.items.len(), si, si.saturating_add(n));
        let room = (dst_len as i64 - di).max(0) as usize;
        let hi = hi.min(lo + room);
        Ok(Some(CopyChunk {
            at: di as usize,
            items: self.items[lo..hi].to_vec(),
            kinds: self.kinds[lo..hi].to_vec(),
        }))
    }

    /// `dst[di:di + len(chunk)] = chunk` (the chunk always fits).
    pub(crate) fn write_chunk(&mut self, chunk: CopyChunk) {
        let end = chunk.at + chunk.items.len();
        self.items[chunk.at..end].clone_from_slice(&chunk.items);
        self.kinds[chunk.at..end].copy_from_slice(&chunk.kinds);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn array(values: &[f64]) -> ElArray {
        ElArray {
            name: "a".into(),
            items: values.iter().map(|v| Value::Num(*v)).collect(),
            kinds: vec![ElemKind::Float; values.len()],
        }
    }

    /// `A[-(el_array_index(i) + 1)]` on a 4-element reversed list.
    #[test]
    fn element_index_table_matches_python_reversed_list() {
        let a = array(&[10.0, 11.0, 12.0, 13.0]);
        let get = |i: f64| a.get(&Value::Num(i)).map(|(v, _)| v.as_f64().unwrap());
        assert_eq!(get(0.0), Ok(10.0));
        assert_eq!(get(3.0), Ok(13.0));
        assert_eq!(get(2.7), Ok(12.0), "int() truncates");
        assert_eq!(get(-0.5), Ok(10.0), "int(-0.5) is 0");
        assert!(get(-1.0).is_err(), "a negative index does not wrap");
        assert!(get(-4.0).is_err(), "-len does not wrap");
        assert!(get(4.0).is_err(), "len is out of range");
        assert!(get(-5.0).is_err(), "-len-1 is out of range");
        assert_eq!(
            get(-1.0).unwrap_err().to_string(),
            get(4.0).unwrap_err().to_string().replace(" 4 ", " -1 "),
            "a negative index fails like one past the end"
        );
        assert!(get(f64::NAN).is_err());
        assert!(get(f64::INFINITY).is_err());
        assert_eq!(
            a.get(&Value::Bool(true)).unwrap().0,
            Value::Num(11.0),
            "int(True) is 1"
        );
    }

    #[test]
    fn negative_write_fails_like_past_the_end() {
        let mut a = array(&[10.0, 11.0]);
        let mut set = |i: f64| a.set(&Value::Num(i), Value::Num(5.0), ElemKind::Float);
        let neg = set(-1.0).unwrap_err().to_string();
        assert!(neg.contains("array index -1 out of range"), "{neg}");
        assert!(set(2.0).is_err(), "past the end still fails");
        assert_eq!(a.items(), &[Value::Num(10.0), Value::Num(11.0)]);
    }

    #[test]
    fn set_max_index_grows_with_int_zeros_at_new_high_indexes() {
        let mut a = array(&[1.5]);
        let grown = a
            .call(ArrayOp::SetMaxIndex, &[Value::Num(3.0)], ElemKind::Unknown)
            .unwrap();
        assert_eq!(grown, Value::Bool(true));
        assert_eq!(a.items().len(), 4);
        assert_eq!(a.items()[0], Value::Num(1.5));
        assert_eq!(a.kinds[1..], [ElemKind::Int; 3]);
        a.call(ArrayOp::SetMaxIndex, &[Value::Num(1.0)], ElemKind::Unknown)
            .unwrap();
        assert_eq!(a.items().len(), 4, "never shrinks");
        assert!(a
            .call(ArrayOp::SetMaxIndex, &[Value::Num(7.5)], ElemKind::Unknown)
            .is_err());
    }

    #[test]
    fn sum_highest_lowest_follow_python_slices() {
        let mut a = array(&[1.0, 5.0, 2.0, 5.0]);
        let call = |a: &mut ElArray, op, s: f64, e: f64| {
            a.call(op, &[Value::Num(s), Value::Num(e)], ElemKind::Unknown)
        };
        assert_eq!(call(&mut a, ArrayOp::Sum, 1.0, 2.0), Ok(Value::Num(7.0)));
        // items[-2:0] is empty.
        assert_eq!(call(&mut a, ArrayOp::Sum, -2.0, -1.0), Ok(Value::Num(0.0)));
        assert_eq!(call(&mut a, ArrayOp::Sum, 0.0, 9.0), Ok(Value::Num(13.0)));
        assert_eq!(
            call(&mut a, ArrayOp::Highest, 0.0, 3.0),
            Ok(Value::Num(5.0))
        );
        assert_eq!(call(&mut a, ArrayOp::Lowest, 1.0, 3.0), Ok(Value::Num(2.0)));
        // end >= len, or start > end: 0.0.
        assert_eq!(
            call(&mut a, ArrayOp::Highest, 0.0, 4.0),
            Ok(Value::Num(0.0))
        );
        assert_eq!(call(&mut a, ArrayOp::Lowest, 3.0, 1.0), Ok(Value::Num(0.0)));
        // max(items[-1:1]) is max([]): Python raises.
        assert!(call(&mut a, ArrayOp::Highest, -1.0, 0.0).is_err());
    }

    #[test]
    fn compensated_sum_matches_cpython_312() {
        let (fl, all) = (PySum::CompensatedFloats, PySum::CompensatedAll);
        let items = [Value::Num(1e16), Value::Num(1.0), Value::Num(-1e16)];
        let floats = [ElemKind::Float; 3];
        assert_eq!(py_sum(&items, &floats, "a", fl), Ok(1.0));
        assert_eq!(py_sum(&items, &floats, "a", all), Ok(1.0));
        // A later int is added without compensation in 3.12/3.13, with it
        // from 3.14: sum([1e16, 1, -1e16]) is 0.0 then 1.0.
        let mixed = [ElemKind::Float, ElemKind::Int, ElemKind::Float];
        assert_eq!(py_sum(&items, &mixed, "a", fl), Ok(0.0));
        assert_eq!(py_sum(&items, &mixed, "a", all), Ok(1.0));
        let tenths = vec![Value::Num(0.1); 10];
        assert_eq!(py_sum(&tenths, &[ElemKind::Float; 10], "a", fl), Ok(1.0));
        assert!(py_sum(&items, &[ElemKind::Unknown; 3], "a", fl).is_err());
        assert!(py_sum(&items, &[ElemKind::Unknown; 3], "a", all).is_err());
        assert_eq!(py_sum(&items, &floats, "a", PySum::Plain), Ok(0.0));
        assert_eq!(py_sum(&items, &mixed, "a", PySum::Plain), Ok(0.0));
        assert_eq!(
            py_sum(&tenths, &[ElemKind::Float; 10], "a", PySum::Plain),
            Ok(0.9999999999999999)
        );
        // 3.12/3.13 leave the float fast path at an int past a C long.
        let huge = [Value::Num(0.5), Value::Num(2f64.powi(70))];
        let fi = [ElemKind::Float, ElemKind::Int];
        assert!(py_sum(&huge, &fi, "a", fl).is_err());
        assert_eq!(py_sum(&huge, &fi, "a", all), Ok(0.5 + 2f64.powi(70)));
    }

    #[test]
    fn sum_mode_follows_the_python_version() {
        assert_eq!(PySum::for_version(3, 9), PySum::Plain);
        assert_eq!(PySum::for_version(3, 11), PySum::Plain);
        assert_eq!(PySum::for_version(3, 12), PySum::CompensatedFloats);
        assert_eq!(PySum::for_version(3, 13), PySum::CompensatedFloats);
        assert_eq!(PySum::for_version(3, 14), PySum::CompensatedAll);
        assert_eq!(PySum::for_version(3, 15), PySum::CompensatedAll);
    }

    #[test]
    fn get_and_set_value_do_not_wrap_and_swallow_index_errors() {
        let mut a = array(&[1.0, 2.0]);
        let get = |a: &mut ElArray, i: f64| {
            a.call(
                ArrayOp::GetValue {
                    bool_default: false,
                },
                &[Value::Num(i)],
                ElemKind::Unknown,
            )
        };
        assert_eq!(get(&mut a, 1.9), Ok(Value::Num(2.0)));
        assert_eq!(get(&mut a, -1.0), Ok(Value::Num(0.0)), "no wrap");
        assert_eq!(get(&mut a, 2.0), Ok(Value::Num(0.0)));
        assert_eq!(get(&mut a, f64::NAN), Ok(Value::Num(0.0)));
        assert!(get(&mut a, f64::INFINITY).is_err(), "OverflowError escapes");
        let set = a
            .call(
                ArrayOp::SetValue { bool_default: true },
                &[Value::Num(5.0), Value::Num(9.0)],
                ElemKind::Int,
            )
            .unwrap();
        assert_eq!(set, Value::Bool(false));
        assert_eq!(a.items(), &[Value::Num(1.0), Value::Num(2.0)]);
    }

    #[test]
    fn copy_index_of_contains_and_ranges() {
        let src = array(&[1.0, 2.0, 3.0]);
        let mut dst = array(&[0.0; 4]);
        let chunk = src
            .copy_chunk(4, &Value::Num(1.0), &Value::Num(2.0), &Value::Num(5.0))
            .unwrap()
            .unwrap();
        dst.write_chunk(chunk);
        let want: Vec<Value> = [0.0, 0.0, 2.0, 3.0].map(Value::Num).to_vec();
        assert_eq!(dst.items(), want.as_slice());
        assert!(src
            .copy_chunk(4, &Value::Num(-1.0), &Value::Num(0.0), &Value::Num(1.0))
            .unwrap()
            .is_none());
        assert_eq!(
            dst.call(ArrayOp::IndexOf, &[Value::Bool(false)], ElemKind::Unknown),
            Ok(Value::Num(0.0)),
            "False == 0"
        );
        assert_eq!(
            dst.call(ArrayOp::Contains, &[Value::str("2")], ElemKind::Unknown),
            Ok(Value::Bool(false))
        );
        dst.call(
            ArrayOp::SetValRange,
            &[Value::Num(-3.0), Value::Num(1.5), Value::Num(7.0)],
            ElemKind::Int,
        )
        .unwrap();
        assert_eq!(dst.items()[..2], [Value::Num(7.0), Value::Num(7.0)]);
        assert_eq!(dst.items()[2], Value::Num(2.0));
    }
}
