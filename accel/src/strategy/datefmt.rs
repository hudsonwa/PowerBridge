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

//! `FormatDate(fmt, dt)`: a port of `_decode_dt_yyyymmdd` and `_fmt_dt` in
//! `pl_transpiler/runtime/_rt_datetime.py`.
//!
//! Every input on which Python raises (a non-finite value, a serial outside
//! `datetime.date`'s range, an invalid YYYYMMDD date) is a refusal here, so the
//! run falls back to Python and fails the same way.

use crate::types::KernelError;

const DAYS: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];
const MONTHS: [&str; 12] = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
];
// `_fmt_dt`'s tokens, longest first: the regex alternation is sorted by length.
const TOKENS: [&str; 22] = [
    "dddd", "MMMM", "yyyy", "ddd", "MMM", "fff", "dd", "MM", "yy", "HH", "hh", "mm", "ss", "tt",
    "d", "M", "y", "H", "h", "m", "s", "t",
];
// The serial day 0 (1899-12-30) as days from 0001-01-01 (`date.toordinal() - 1`).
const SERIAL_EPOCH: i64 = 693_593;

fn refuse(what: &str) -> KernelError {
    KernelError::unsupported(format!("FormatDate {what}"))
}

/// Days since 1970-01-01 of a proleptic Gregorian date (H. Hinnant).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Python `f"{n:0{width}d}"`: the sign counts toward the width.
fn pad(n: i64, width: usize) -> String {
    if n < 0 {
        format!("-{:0w$}", -n, w = width.saturating_sub(1))
    } else {
        format!("{n:0width$}")
    }
}

/// `_decode_dt_yyyymmdd(dt)` -> `(y, mo, d, h, mi, s)`.
fn decode(dt: f64) -> Result<[i64; 6], KernelError> {
    // int(dt) raises on nan/inf; past 2**53 the i64 cast would not be exact.
    if !dt.is_finite() || dt.abs() >= 9.0e15 {
        return Err(refuse("datetime is out of range"));
    }
    let ip = dt.trunc();
    let frac = dt - ip;
    let ip = ip as i64;
    let tot = (frac * 86400.0).round_ties_even() as i64;
    let (h, mi, s) = (
        tot.div_euclid(3600),
        tot.rem_euclid(3600).div_euclid(60),
        tot.rem_euclid(60),
    );
    let (y, mo, d) = if -693_594 < ip && ip < 10_000_000 {
        // date(1899, 12, 30) + timedelta(days=ip) must stay in years 1..9999.
        if !(-SERIAL_EPOCH..=2_958_465).contains(&ip) {
            return Err(refuse("serial is outside years 1..9999"));
        }
        civil_from_days(days_from_civil(1899, 12, 30) + ip)
    } else {
        (
            ip.div_euclid(10_000),
            ip.div_euclid(100).rem_euclid(100),
            ip.rem_euclid(100),
        )
    };
    Ok([y, mo, d, h, mi, s])
}

/// `pl_formatdate(fmt, dt)` for a string `fmt` and a numeric `dt`.
pub(crate) fn format_date(fmt: &str, dt: f64) -> Result<String, KernelError> {
    let [y, mo, d, h, mi, s] = decode(dt)?;
    // calendar.weekday builds a datetime.date, which raises on a bad date.
    if !(1..=9999).contains(&y) || !(1..=12).contains(&mo) || d < 1 || d > days_in_month(y, mo) {
        return Err(refuse("date is not a valid calendar date"));
    }
    // 1970-01-01 was a Thursday (Monday = 0).
    let wd = (days_from_civil(y, mo, d) + 3).rem_euclid(7) as usize;
    let h12 = match h.rem_euclid(12) {
        0 => 12,
        v => v,
    };
    let tt = if h < 12 { "AM" } else { "PM" };
    let day = DAYS[wd];
    let month = MONTHS[(mo - 1) as usize];
    let chars: Vec<char> = fmt.chars().collect();
    let mut out = String::with_capacity(fmt.len() + 8);
    let mut i = 0;
    while i < chars.len() {
        let token = TOKENS.iter().find(|t| {
            let n = t.len();
            i + n <= chars.len() && t.chars().eq(chars[i..i + n].iter().copied())
        });
        let Some(token) = token else {
            out.push(chars[i]);
            i += 1;
            continue;
        };
        let text = match *token {
            "dddd" => day.to_string(),
            "ddd" => day[..3].to_string(),
            "dd" => pad(d, 2),
            "d" => d.to_string(),
            "MMMM" => month.to_string(),
            "MMM" => month[..3].to_string(),
            "MM" => pad(mo, 2),
            "M" => mo.to_string(),
            "yyyy" => pad(y, 4),
            "yy" => pad(y.rem_euclid(100), 2),
            "y" => y.rem_euclid(100).to_string(),
            "HH" => pad(h, 2),
            "H" => h.to_string(),
            "hh" => pad(h12, 2),
            "h" => h12.to_string(),
            "mm" => pad(mi, 2),
            "m" => mi.to_string(),
            "ss" => pad(s, 2),
            "s" => s.to_string(),
            "fff" => "000".to_string(),
            "tt" => tt.to_string(),
            _ => tt[..1].to_string(),
        };
        out.push_str(&text);
        i += token.len();
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// (fmt, dt, Python `pl_formatdate(fmt, dt)`), generated by
    /// `tools/gen_formatdate_table.py`.
    const TABLE: &[(&str, f64, &str)] = include!("fixtures/formatdate_table.txt");

    #[test]
    fn matches_the_python_table() {
        for (fmt, dt, want) in TABLE {
            assert_eq!(format_date(fmt, *dt).unwrap(), *want, "{fmt:?} {dt}");
        }
    }

    #[test]
    fn refuses_where_python_raises() {
        for dt in [
            f64::NAN,
            f64::INFINITY,
            2_958_466.0,
            20_241_301.0,
            20_240_230.0,
        ] {
            assert!(format_date("yyyy", dt).is_err(), "{dt}");
        }
    }

    #[test]
    fn pad_counts_the_sign_like_python() {
        assert_eq!(pad(-5, 2), "-5");
        assert_eq!(pad(-5, 3), "-05");
        assert_eq!(pad(7, 4), "0007");
    }
}
