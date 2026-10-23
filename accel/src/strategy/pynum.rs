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

//! Numeric operators and builtins with the Python runtime's semantics.
//!
//! The Python path is the reference: the codegen emits plain Python `a / b`,
//! `a % b`, `int(x)`, `x ** 2`, `max(...)` and `min(...)`. Every function
//! here returns exactly what that Python expression returns. Where Python
//! raises instead (a zero divisor, `int()` of a non-finite value, a float
//! overflow in `**`, `max`/`min` of fewer than two values), the kernel
//! refuses the run so the Python path runs and reports the error itself.

use super::compile::{Binop, CallOp, Instr};
use super::specialize::stack_delta;
use super::value::Value;
use crate::types::KernelError;

fn refuse(what: &str) -> KernelError {
    KernelError::unsupported(format!("{what} (the Python path raises; it runs instead)"))
}

/// Python `a / b` on floats.
#[inline(always)]
pub(crate) fn py_div(a: f64, b: f64) -> Result<f64, KernelError> {
    if b == 0.0 {
        return Err(refuse("division by zero"));
    }
    Ok(a / b)
}

/// Python `a % b` on floats (CPython `float_rem`): the result takes the sign
/// of the divisor, so `Mod(-3, 4)` is `1`, not C `fmod`'s `-3`.
///
/// This matches the Python path; the sign for negative operands has not been
/// checked against MultiCharts.
#[inline(always)]
pub(crate) fn py_mod(a: f64, b: f64) -> Result<f64, KernelError> {
    if b == 0.0 {
        return Err(refuse("modulo by zero"));
    }
    let m = a % b;
    Ok(if m != 0.0 {
        if (b < 0.0) != (m < 0.0) {
            m + b
        } else {
            m
        }
    } else {
        0.0_f64.copysign(b)
    })
}

/// A numeric binary operator with Python semantics.
#[inline(always)]
pub(crate) fn binop_num(op: Binop, a: f64, b: f64) -> Result<f64, KernelError> {
    match op {
        Binop::Add => Ok(a + b),
        Binop::Sub => Ok(a - b),
        Binop::Mul => Ok(a * b),
        Binop::Div => py_div(a, b),
        Binop::Mod => py_mod(a, b),
    }
}

/// `IntPortion(x)`, emitted as Python `int(x)`: truncation toward zero.
pub(crate) fn py_int(a: f64) -> Result<f64, KernelError> {
    if !a.is_finite() {
        return Err(refuse("IntPortion of a non-finite value"));
    }
    Ok(a.trunc())
}

/// `Square(x)`, emitted as Python `x ** 2`, which raises on overflow.
pub(crate) fn py_square(a: f64) -> Result<f64, KernelError> {
    let r = a * a;
    if a.is_finite() && !r.is_finite() {
        return Err(refuse("Square overflow"));
    }
    Ok(r)
}

/// Exact powers of ten that Python's `10 ** n` gives for an int `n` in 0..=22.
const POW10: [f64; 23] = [
    1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14, 1e15, 1e16,
    1e17, 1e18, 1e19, 1e20, 1e21, 1e22,
];

/// `Round(x, n)` (`el_round`) or, with `decimals` `None`, `Round(x)`
/// (`el_round0`): half away from zero, on `x * 10 ** n`.
///
/// Python raises where `math.floor`/`math.ceil` see a NaN or infinity, where
/// `10 ** n` overflows, and where it underflows to `0.0` (the division). An
/// integral `n` above 22 refuses: `10 ** n` is then an exact Python int that
/// `f64` cannot hold, so the division could round differently.
pub(crate) fn py_round(x: f64, decimals: Option<f64>) -> Result<f64, KernelError> {
    let factor = match decimals {
        None => 1.0,
        Some(n) if n.fract() == 0.0 && (0.0..=22.0).contains(&n) => POW10[n as usize],
        Some(n) if n.fract() == 0.0 && n > 22.0 => {
            return Err(refuse("Round precision above 22"));
        }
        // A negative or fractional n: Python's float pow.
        Some(n) => 10f64.powf(n),
    };
    if factor.is_infinite() {
        return Err(refuse("Round precision overflow"));
    }
    let scaled = x * factor;
    let k = if x >= 0.0 {
        (scaled + 0.5).floor()
    } else {
        (scaled - 0.5).ceil()
    };
    if !k.is_finite() {
        return Err(refuse("Round of a non-finite value"));
    }
    // math.floor/ceil return an int, which has no negative zero.
    let k = k + 0.0;
    match decimals {
        None => Ok(k),
        Some(_) if factor == 0.0 => Err(refuse("Round precision underflow")),
        Some(_) => Ok(k / factor),
    }
}

/// `Sign(x)`, emitted as `1 if x > 0 else (-1 if x < 0 else 0)`.
pub(crate) fn py_sign(a: f64) -> f64 {
    if a > 0.0 {
        1.0
    } else if a < 0.0 {
        -1.0
    } else {
        0.0
    }
}

/// `MaxList(...)` / `MinList(...)`, emitted as Python `max(...)` / `min(...)`:
/// the first argument wins unless a later one compares strictly greater
/// (less), which fixes the result for NaN and signed zeros. One argument
/// raises in Python (`max(5)` is not iterable), so fewer than two refuse.
pub(crate) fn py_extreme(args: &[f64], max: bool) -> Result<f64, KernelError> {
    if args.len() < 2 {
        return Err(refuse("MaxList/MinList with fewer than two values"));
    }
    let mut m = args[0];
    for &x in &args[1..] {
        if (max && x > m) || (!max && x < m) {
            m = x;
        }
    }
    Ok(m)
}

/// Whether `instr` can raise on the Python path for some operand values, so
/// the kernel may refuse it at run time: a `/` or `mod` whose divisor is not a
/// non-zero constant, `IntPortion`, `Square`, `Round`, or `MaxList`/`MinList` of fewer
/// than two values. Such an instruction must run on every bar Python runs it:
/// it is never deleted as dead code. `prev` is the preceding instruction (the
/// divisor push for a plain `Binop`), when known.
pub(crate) fn may_raise(instr: &Instr, prev: Option<&Instr>) -> bool {
    match instr {
        Instr::Call(CallOp::IntPortion | CallOp::Square | CallOp::Round, _) => true,
        _ => divides_unsafely(instr, prev, &[]),
    }
}

/// Whether `instr` is a `/` or `mod` that can see a zero divisor, or a
/// `MaxList`/`MinList` of fewer than two values. A divisor pushed from a
/// non-zero number literal or input (`inputs`, already resolved) is safe.
fn divides_unsafely(instr: &Instr, prev: Option<&Instr>, inputs: &[Value]) -> bool {
    let divides = |op: &Binop| matches!(op, Binop::Div | Binop::Mod);
    let non_zero = |v: Option<&Value>| matches!(v, Some(Value::Num(d)) if *d != 0.0);
    match instr {
        Instr::Binop(op) if divides(op) => match prev {
            Some(Instr::PushNum(d)) => *d == 0.0,
            Some(Instr::PushInput(slot)) => !non_zero(inputs.get(*slot)),
            _ => true,
        },
        Instr::BinopC { op, rhs }
        | Instr::SeriesBinopC { op, rhs, .. }
        | Instr::VarBinopC { op, rhs, .. } => divides(op) && *rhs == 0.0,
        Instr::BinopCLhs { op, .. }
        | Instr::SeriesBinopCLhs { op, .. }
        | Instr::VarBinopCLhs { op, .. } => divides(op),
        Instr::Call(CallOp::MaxList | CallOp::MinList, n) => *n < 2,
        _ => false,
    }
}

/// Whether the one-value operand ending just before `pc` may read a variable.
/// Bar data, literals and inputs are finite, and nothing below `pc` can make
/// them NaN or infinite unless an unsafe division (checked separately) does.
/// A variable or array element can grow without bound across bars
/// (`C = Square(C)`), so it
/// counts as unsafe, as does an operand this walk cannot delimit.
fn operand_reads_var(instrs: &[Instr], pc: usize) -> bool {
    let mut need = 1;
    let mut i = pc;
    while need > 0 {
        let Some(j) = i.checked_sub(1) else {
            return true;
        };
        i = j;
        let ins = &instrs[i];
        if matches!(
            ins,
            Instr::PushVar(_)
                | Instr::PushVarSeries(_)
                | Instr::LoadVar(_)
                | Instr::BarRefVar(_)
                | Instr::BarRefVarConst { .. }
                | Instr::VarBinopC { .. }
                | Instr::VarBinopCLhs { .. }
                | Instr::VarCompareC { .. }
                | Instr::VarCompareCLhs { .. }
                | Instr::ArrayGet(_)
                | Instr::ArrayCall { .. }
        ) {
            return true;
        }
        match stack_delta(ins) {
            Some((pop, push)) if pop > 0 || push > 0 => need = need - push + pop,
            _ => return true,
        }
    }
    false
}

/// Whether any instruction of `instrs` can raise on the Python path for some
/// bar, so a prefix skip would hide the error: an unsafe division (see
/// [`divides_unsafely`]), `IntPortion`/`Square` of a value that may be
/// non-finite or unbounded, i.e. one that reads a variable, or any `Round`. With no unsafe
/// division, `IntPortion(Time / 100)` or `Square(Close - Open)` only see
/// finite bar data and cannot raise.
pub(crate) fn program_may_raise(instrs: &[Instr], inputs: &[Value]) -> bool {
    instrs.iter().enumerate().any(|(pc, instr)| {
        let prev = pc.checked_sub(1).map(|p| &instrs[p]);
        divides_unsafely(instr, prev, inputs)
            || (matches!(instr, Instr::Call(CallOp::IntPortion | CallOp::Square, 1))
                && operand_reads_var(instrs, pc))
            || matches!(instr, Instr::Call(CallOp::IntPortion | CallOp::Square, n) if *n != 1)
            // The precision operand can overflow or underflow `10 ** n`.
            || matches!(instr, Instr::Call(CallOp::Round, _))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bits(r: Result<f64, KernelError>) -> u64 {
        r.unwrap().to_bits()
    }

    #[test]
    fn mod_follows_divisor_sign() {
        // Values from CPython `a % b`.
        let cases = [
            (-3.0, 4.0, 1.0),
            (3.0, -4.0, -1.0),
            (-3.0, -4.0, -3.0),
            (7.0, 4.0, 3.0),
            (-7.5, 2.0, 0.5),
            (7.5, -2.0, -0.5),
            (-0.25, 1.0, 0.75),
            (-3.0, f64::INFINITY, f64::INFINITY),
            (3.0, f64::NEG_INFINITY, f64::NEG_INFINITY),
            (3.0, f64::INFINITY, 3.0),
        ];
        for (a, b, want) in cases {
            assert_eq!(py_mod(a, b).unwrap(), want, "{a} % {b}");
        }
        assert!(py_mod(f64::INFINITY, 3.0).unwrap().is_nan());
    }

    #[test]
    fn mod_zero_result_takes_divisor_sign() {
        assert_eq!(bits(py_mod(-4.0, 4.0)), 0.0_f64.to_bits());
        assert_eq!(bits(py_mod(4.0, -4.0)), (-0.0_f64).to_bits());
        assert_eq!(bits(py_mod(-0.0, 4.0)), 0.0_f64.to_bits());
    }

    #[test]
    fn zero_divisor_refuses() {
        for b in [0.0, -0.0] {
            assert!(py_mod(1.0, b).is_err());
            assert!(py_div(1.0, b).is_err());
            assert!(binop_num(Binop::Div, 0.0, b).is_err());
            assert!(binop_num(Binop::Mod, -3.0, b).is_err());
        }
        assert_eq!(binop_num(Binop::Div, -3.0, 4.0).unwrap(), -0.75);
    }

    #[test]
    fn round_matches_el_round() {
        // Values from the runtime's el_round / el_round0 under CPython.
        let cases: [(f64, f64, f64); 16] = [
            (2.5, 0.0, 3.0),
            (-2.5, 0.0, -3.0),
            (0.5, 0.0, 1.0),
            (-0.5, 0.0, -1.0),
            (1.005, 2.0, 1.0),
            (-1.005, 2.0, -1.0),
            (2.675, 2.0, 2.68),
            (0.125, 2.0, 0.13),
            (-0.125, 2.0, -0.13),
            (1234.5, -2.0, 1200.0),
            (1250.0, -2.0, 1300.0),
            (-1250.0, -2.0, -1300.0),
            (-1249.99, -2.0, -1200.0),
            (-15.0, -1.0, -20.0),
            (1.23456, 1.5, 1.233_288_287_465_667_9),
            (-1.23456, 1.5, -1.233_288_287_465_667_9),
        ];
        for (x, n, want) in cases {
            assert_eq!(
                bits(py_round(x, Some(n))),
                want.to_bits(),
                "Round({x}, {n})"
            );
        }
        // math.ceil(-0.7) is the int 0: no negative zero.
        assert_eq!(bits(py_round(-0.2, Some(0.0))), 0f64.to_bits());
        assert_eq!(bits(py_round(-0.4, None)), 0f64.to_bits());
        assert_eq!(py_round(-2.5, None).unwrap(), -3.0);
        assert_eq!(py_round(0.499_999_999_999_999_94, None).unwrap(), 1.0);
        // OverflowError, ZeroDivisionError, ValueError on the Python path.
        assert!(py_round(1.0, Some(400.0)).is_err());
        assert!(py_round(1.0, Some(-400.0)).is_err());
        assert!(py_round(f64::INFINITY, Some(2.0)).is_err());
        assert!(py_round(f64::NAN, Some(0.0)).is_err());
        assert!(py_round(1e308, Some(2.0)).is_err());
        // An exact int 10 ** 23 is refused rather than approximated.
        assert!(py_round(7.0, Some(23.0)).is_err());
    }

    #[test]
    fn int_square_sign_extreme() {
        assert_eq!(py_int(-2.7).unwrap(), -2.0);
        assert_eq!(py_int(2.7).unwrap(), 2.0);
        assert!(py_int(f64::NAN).is_err());
        assert!(py_int(f64::NEG_INFINITY).is_err());
        assert_eq!(py_square(-1.5).unwrap(), 2.25);
        assert!(py_square(1e200).is_err());
        assert_eq!(py_square(f64::INFINITY).unwrap(), f64::INFINITY);
        assert_eq!(py_sign(-0.0), 0.0);
        assert_eq!(py_sign(f64::NAN), 0.0);
        assert_eq!(py_sign(-2.0), -1.0);
        assert!(py_extreme(&[5.0], true).is_err());
        assert!(py_extreme(&[f64::NAN, 1.0], true).unwrap().is_nan());
        assert_eq!(py_extreme(&[1.0, f64::NAN], true).unwrap(), 1.0);
        assert_eq!(bits(py_extreme(&[-0.0, 0.0], true)), (-0.0_f64).to_bits());
        assert_eq!(bits(py_extreme(&[0.0, -0.0], false)), 0.0_f64.to_bits());
        assert_eq!(py_extreme(&[-3.0, -1.0, -2.0], false).unwrap(), -3.0);
    }

    #[test]
    fn may_raise_marks_variable_divisors_and_partial_builtins() {
        let four = Instr::PushNum(4.0);
        let zero = Instr::PushNum(0.0);
        assert!(!may_raise(&Instr::Binop(Binop::Mod), Some(&four)));
        assert!(may_raise(&Instr::Binop(Binop::Mod), Some(&zero)));
        assert!(may_raise(
            &Instr::Binop(Binop::Div),
            Some(&Instr::PushVar(0))
        ));
        assert!(may_raise(&Instr::Binop(Binop::Div), None));
        assert!(!may_raise(&Instr::Binop(Binop::Mul), None));
        let c = |rhs| Instr::BinopC {
            op: Binop::Div,
            rhs,
        };
        assert!(may_raise(&c(0.0), None));
        assert!(!may_raise(&c(2.0), None));
        assert!(may_raise(&Instr::Call(CallOp::IntPortion, 1), None));
        assert!(may_raise(&Instr::Call(CallOp::MaxList, 1), None));
        assert!(!may_raise(&Instr::Call(CallOp::MaxList, 2), None));
        assert!(!may_raise(&Instr::Call(CallOp::AbsValue, 1), None));
    }
}
