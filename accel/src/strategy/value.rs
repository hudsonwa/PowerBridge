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

//! Runtime values for the IR interpreter.

use super::arrays::{py_sum_by, python_sum};
use super::compile::ElemKind;
use crate::types::{Fill, KernelError};

/// A flat runtime value: a bare `f64` scalar (and a one-byte boolean) in the
/// common numeric path, with a boxed — and therefore narrow — fallback for the
/// genuinely non-numeric values (strings and materialised series) that a
/// numeric strategy never constructs.
///
/// Keeping `Num`/`Bool` inline and boxing only `NonNum` makes `Value` a
/// 16-byte type instead of the previous 32 bytes, so the operand stack and the
/// per-bar variable history carry half the bytes on the hot arithmetic path.
/// Semantics are unchanged: the fallback is only reached for the same string /
/// series values the unboxed enum already held, and every variant still derives
/// deep `Clone`/`PartialEq`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Value {
    Num(f64),
    Bool(bool),
    NonNum(Box<NonNumeric>),
}

/// The boxed, non-numeric payload of [`Value`] — the narrow fallback a numeric
/// strategy does not touch.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum NonNumeric {
    Str(String),
    Series(Vec<f64>),
}

impl Value {
    #[inline]
    pub(crate) fn str(s: impl Into<String>) -> Self {
        Self::NonNum(Box::new(NonNumeric::Str(s.into())))
    }

    #[inline]
    pub(crate) fn series(v: Vec<f64>) -> Self {
        Self::NonNum(Box::new(NonNumeric::Series(v)))
    }

    #[inline]
    pub(crate) fn is_str(&self) -> bool {
        matches!(self, Self::NonNum(b) if matches!(**b, NonNumeric::Str(_)))
    }

    pub(crate) fn as_str(&self) -> Option<&str> {
        match self {
            Self::NonNum(b) => match &**b {
                NonNumeric::Str(s) => Some(s),
                NonNumeric::Series(_) => None,
            },
            _ => None,
        }
    }

    pub(crate) fn is_truthy(&self) -> bool {
        match self {
            Self::Num(n) => *n != 0.0,
            Self::Bool(b) => *b,
            Self::NonNum(b) => match &**b {
                NonNumeric::Str(s) => !s.is_empty(),
                NonNumeric::Series(s) => !s.is_empty(),
            },
        }
    }

    pub(crate) fn as_f64(&self) -> Result<f64, KernelError> {
        match self {
            Self::Num(n) => Ok(*n),
            Self::Bool(true) => Ok(1.0),
            Self::Bool(false) => Ok(0.0),
            Self::NonNum(b) => match &**b {
                NonNumeric::Str(s) => s.parse::<f64>().map_err(|_| {
                    KernelError::unsupported(format!("cannot convert {s:?} to number"))
                }),
                NonNumeric::Series(_) => Err(KernelError::unsupported(
                    "cannot convert a series to a scalar number",
                )),
            },
        }
    }

    pub(crate) fn into_string(self) -> String {
        match self {
            Self::Num(n) => python_str_num(n),
            Self::Bool(true) => "True".to_string(),
            Self::Bool(false) => "False".to_string(),
            Self::NonNum(b) => match *b {
                NonNumeric::Str(s) => s,
                NonNumeric::Series(_) => "<series>".to_string(),
            },
        }
    }
}

fn python_str_num(n: f64) -> String {
    if n.is_nan() {
        return "nan".to_string();
    }
    if n.is_infinite() {
        return if n.is_sign_positive() {
            "inf".into()
        } else {
            "-inf".into()
        };
    }
    if n.fract() == 0.0 && n.abs() < 1e15 {
        format!("{}", n as i64)
    } else {
        format!("{n}")
    }
}

/// Python `f"{x:.{decimals}f}"` (used by codegen's NumToStr expansion).
pub(crate) fn num_to_str(x: f64, decimals: i64) -> String {
    let prec = decimals.max(0) as usize;
    format!("{x:.prec$}")
}

/// CPython `float.hex()` for finite normals/zeros (the fill-digest format).
#[allow(dead_code)]
pub(crate) fn python_float_hex(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x.is_infinite() {
        return if x.is_sign_positive() {
            "inf".into()
        } else {
            "-inf".into()
        };
    }
    let bits = x.to_bits();
    let sign = if bits & (1u64 << 63) != 0 { "-" } else { "" };
    let exp_bits = ((bits >> 52) & 0x7ff) as i32;
    let frac = bits & 0x000f_ffff_ffff_ffff;
    if exp_bits == 0 {
        if frac == 0 {
            return format!("{sign}0x0.0p+0");
        }
        let shift = frac.leading_zeros() - 11; // 64 - 1 - 52 = 11 leading bits above mantissa
        let normalized = frac << shift;
        let mant = normalized & 0x000f_ffff_ffff_ffff;
        let e = -1022 - (shift as i32);
        return format!("{sign}0x1.{mant:013x}p{e:+}");
    }
    let e = exp_bits - 1023;
    format!("{sign}0x1.{frac:013x}p{e:+}")
}

/// One canonical fill line in the `pl_oracle.canonical_fill_line` shape.
#[allow(dead_code)]
pub(crate) fn canonical_fill_line(fill: &Fill) -> String {
    format!(
        "{}\t{}\t{}\t{}\t{}\t{}",
        fill.fill_type,
        fill.signal,
        fill.date,
        fill.time,
        python_float_hex(fill.price),
        fill.contracts
    )
}

/// A series argument to a series function, resolved without copying the
/// growing per-bar history when possible (the lookback path was
/// quadratic because every bar cloned the full history into a fresh `Vec<f64>`).
///
/// * [`SeriesView::Slice`] borrows a primary data-series column (`self.close`,
///   `self.open`, …) whose current bar was already pushed in `begin_bar`.
/// * [`SeriesView::Chained`] borrows a numeric variable's history and appends
///   its just-assigned current value (`history + current`), matching the
///   `eval_ident` series-context materialisation bit-for-bit.
/// * [`SeriesView::Owned`] is the fallback for values that still need a
///   materialised copy (date/time columns, non-numeric variables).
pub(crate) enum SeriesView<'a> {
    Slice(&'a [f64]),
    Chained(&'a [f64], f64),
    Owned(Vec<f64>),
}

impl SeriesView<'_> {
    #[inline]
    fn len(&self) -> usize {
        match self {
            SeriesView::Slice(s) => s.len(),
            SeriesView::Chained(h, _) => h.len() + 1,
            SeriesView::Owned(v) => v.len(),
        }
    }

    #[inline]
    fn at(&self, i: usize) -> f64 {
        match self {
            SeriesView::Slice(s) => s[i],
            SeriesView::Chained(h, t) => {
                if i < h.len() {
                    h[i]
                } else {
                    *t
                }
            }
            SeriesView::Owned(v) => v[i],
        }
    }

    fn first_valid(&self) -> usize {
        for i in 0..self.len() {
            if self.at(i) != 0.0 {
                return i;
            }
        }
        self.len()
    }
}

/// `pl_average`: SMA of the last `length` valid values, then `f32`.
pub(crate) fn pl_average(
    series: &SeriesView,
    length: f64,
    kind: ElemKind,
    narrow: fn(f64) -> f64,
) -> Result<f64, KernelError> {
    if series.len() == 0 || length <= 0.0 {
        return Ok(0.0);
    }
    let start = series.first_valid();
    let n_valid = series.len() - start;
    // Python compares the available count with the original numeric length
    // before attempting its slice. Do not truncate a fractional length.
    if n_valid == 0 || (n_valid as f64) < length {
        return Ok(0.0);
    }
    if !length.is_finite() || length.fract() != 0.0 || length > usize::MAX as f64 {
        return Err(KernelError::unsupported(format!(
            "Average length {length} cannot be used as a Python slice index"
        )));
    }
    let length_n = length as usize;
    let first = series.len() - length_n;
    // Python: `sum(window)`, whose rounding depends on the interpreter.
    let sum = py_sum_by(
        length_n,
        |i| Ok(series.at(first + i)),
        |_| kind,
        "Average",
        "a series",
        python_sum(),
    )?;
    Ok(narrow(sum / length_n as f64))
}

fn cross_prior_relation(series_a: &SeriesView, series_b: &SeriesView, start: usize) -> i8 {
    let mut i = series_a.len().saturating_sub(2);
    while i >= start && i < series_a.len() && i < series_b.len() {
        let a = series_a.at(i);
        let b = series_b.at(i);
        if a < b {
            return -1;
        }
        if a > b {
            return 1;
        }
        if i == 0 {
            break;
        }
        i -= 1;
    }
    0
}

/// First bar of a cross comparison, mirroring Python `_cross_operands`.
/// Leading zeros (PAD element, indicator warm-up) are skipped only on a side
/// that has a nonzero value; an all-zero side is a zero level, not a warm-up.
fn cross_start(series_a: &SeriesView, series_b: &SeriesView) -> usize {
    let start_a = series_a.first_valid();
    let start_b = series_b.first_valid();
    if start_a == series_a.len() {
        start_b
    } else if start_b == series_b.len() {
        start_a
    } else {
        start_a.max(start_b)
    }
}

pub(crate) fn pl_crosses_above(series_a: &SeriesView, series_b: &SeriesView) -> bool {
    if series_a.len() < 2 || series_b.len() < 2 {
        return false;
    }
    let start = cross_start(series_a, series_b);
    if series_a.len().saturating_sub(start) < 2 {
        return false;
    }
    series_a.at(series_a.len() - 1) > series_b.at(series_b.len() - 1)
        && cross_prior_relation(series_a, series_b, start) < 0
}

pub(crate) fn pl_crosses_below(series_a: &SeriesView, series_b: &SeriesView) -> bool {
    if series_a.len() < 2 || series_b.len() < 2 {
        return false;
    }
    let start = cross_start(series_a, series_b);
    if series_a.len().saturating_sub(start) < 2 {
        return false;
    }
    series_a.at(series_a.len() - 1) < series_b.at(series_b.len() - 1)
        && cross_prior_relation(series_a, series_b, start) > 0
}

#[cfg(test)]
mod cross_tests {
    use super::*;

    #[test]
    fn crosses_an_all_zero_level() {
        let x = [0.0, 5.0, -1.0, -2.0, 3.0];
        let zeros = [0.0; 5];
        assert!(pl_crosses_above(
            &SeriesView::Slice(&x),
            &SeriesView::Slice(&zeros)
        ));
        assert!(pl_crosses_below(
            &SeriesView::Slice(&x[..3]),
            &SeriesView::Slice(&zeros[..3])
        ));
        // The leading pad zero on x is still skipped.
        assert!(!pl_crosses_above(
            &SeriesView::Slice(&x[..2]),
            &SeriesView::Slice(&zeros[..2])
        ));
        assert!(!pl_crosses_above(
            &SeriesView::Slice(&zeros),
            &SeriesView::Slice(&zeros)
        ));
    }

    #[test]
    fn warm_up_side_still_starts_at_first_value() {
        let fast = [0.0, 10.0, 11.0, 12.0];
        let slow = [0.0, 0.0, 0.0, 11.5];
        assert!(!pl_crosses_above(
            &SeriesView::Slice(&fast),
            &SeriesView::Slice(&slow)
        ));
        assert!(!pl_crosses_below(
            &SeriesView::Slice(&fast),
            &SeriesView::Slice(&slow)
        ));
    }
}
