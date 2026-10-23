//! The computer clock behind `CurrentTime_s`, `CurrentDate_s` and
//! `ComputerDateTime`.
//!
//! The Python runtime reads `_rt_datetime._clock_now()` (local
//! `datetime.now()`) on every call. The kernel calls that same function
//! through the interpreter, so both paths share one time source, and a test
//! that patches `_clock_now` pins the native run too. Rust unit tests, which
//! run without `libpython`, inject a fixed [`Clock`] instead.

#[cfg(not(test))]
use pyo3::prelude::*;

use crate::types::KernelError;

use super::compile::ClockWord;

/// The fields of a Python `datetime` the clock words read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ClockNow {
    pub(crate) year: i64,
    pub(crate) month: i64,
    pub(crate) day: i64,
    pub(crate) hour: i64,
    pub(crate) minute: i64,
    pub(crate) second: i64,
    pub(crate) microsecond: i64,
}

/// A source of the current local time.
pub(crate) type Clock = fn() -> Result<ClockNow, KernelError>;

/// `pl_transpiler.runtime._rt_datetime._clock_now()`, looked up on each call.
#[cfg(not(test))]
pub(crate) fn python_clock() -> Result<ClockNow, KernelError> {
    Python::with_gil(|py| {
        let now = py
            .import("pl_transpiler.runtime._rt_datetime")?
            .getattr("_clock_now")?
            .call0()?;
        let field = |name: &str| -> PyResult<i64> { now.getattr(name)?.extract() };
        Ok(ClockNow {
            year: field("year")?,
            month: field("month")?,
            day: field("day")?,
            hour: field("hour")?,
            minute: field("minute")?,
            second: field("second")?,
            microsecond: field("microsecond")?,
        })
    })
    .map_err(|e: PyErr| KernelError::unsupported(format!("computer clock: {e}")))
}

/// The test binary has no `libpython` (and its Python data symbols cannot
/// load), so tests that read the clock set one with `set_clock`.
#[cfg(test)]
pub(crate) fn python_clock() -> Result<ClockNow, KernelError> {
    Err(KernelError::unsupported(
        "computer clock: no Python in unit tests",
    ))
}

/// Days from 1970-01-01 to the proleptic Gregorian date `y-m-d`.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The value the runtime's function for `word` returns at `now`.
pub(crate) fn clock_value(word: ClockWord, now: &ClockNow) -> f64 {
    match word {
        // pl_currenttime_s: hour * 10000 + minute * 100 + second.
        ClockWord::TimeS => (now.hour * 10000 + now.minute * 100 + now.second) as f64,
        // pl_currentdate_s -> pl_currentdate: (year - 1900) * 10000 + ...
        ClockWord::DateS => ((now.year - 1900) * 10000 + now.month * 100 + now.day) as f64,
        // _ole_days: delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400.0
        ClockWord::ComputerDateTime => {
            let days =
                days_from_civil(now.year, now.month, now.day) - days_from_civil(1899, 12, 30);
            let seconds = now.hour * 3600 + now.minute * 60 + now.second;
            days as f64 + (seconds as f64 + now.microsecond as f64 / 1e6) / 86400.0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(year: i64, month: i64, day: i64, h: i64, m: i64, s: i64, us: i64) -> ClockNow {
        ClockNow {
            year,
            month,
            day,
            hour: h,
            minute: m,
            second: s,
            microsecond: us,
        }
    }

    #[test]
    fn clock_words_match_the_runtime() {
        // Values from the runtime's functions with `_clock_now` patched.
        let now = at(2024, 3, 5, 9, 7, 3, 250_000);
        assert_eq!(clock_value(ClockWord::TimeS, &now), 90703.0);
        assert_eq!(clock_value(ClockWord::DateS, &now), 1_240_305.0);
        assert_eq!(
            clock_value(ClockWord::ComputerDateTime, &now).to_bits(),
            45_356.379_898_726_85_f64.to_bits()
        );
        let midnight = at(1899, 12, 30, 0, 0, 0, 0);
        assert_eq!(clock_value(ClockWord::ComputerDateTime, &midnight), 0.0);
        assert_eq!(clock_value(ClockWord::DateS, &midnight), -8_770.0);
        let late = at(2000, 2, 29, 23, 59, 59, 999_999);
        assert_eq!(clock_value(ClockWord::TimeS, &late), 235_959.0);
        assert_eq!(clock_value(ClockWord::DateS, &late), 1_000_229.0);
        assert_eq!(
            clock_value(ClockWord::ComputerDateTime, &late).to_bits(),
            36_585.999_999_999_985_f64.to_bits()
        );
    }
}
