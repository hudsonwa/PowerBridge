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

use std::collections::BTreeMap;

use crate::types::{PositionView, SessionEngine, TraceReport};

use super::fill::FillEngine;

/// Format one emitted bar into a report row.
///
/// Freezes `report.columns` from the first bar's keys; later extra keys are
/// ignored and later missing keys render as `"0"`. Does not retain the row —
/// the loop driver decides whether to push it or stream it to a sink.
pub(crate) fn format_trace_row(
    report: &mut TraceReport,
    mut values: BTreeMap<String, Option<String>>,
    engine: &FillEngine,
    emit_gta6: bool,
) -> Vec<String> {
    inject_engine_values(&mut values, engine, emit_gta6);
    if report.columns.is_empty() {
        report.columns.extend(values.keys().cloned());
    }
    report
        .columns
        .iter()
        .map(|column| {
            values
                .get(column)
                .and_then(Clone::clone)
                .unwrap_or_else(|| "0".to_owned())
        })
        .collect()
}

fn inject_engine_values(
    values: &mut BTreeMap<String, Option<String>>,
    engine: &FillEngine,
    emit_gta6: bool,
) {
    let position = engine.position();
    insert_i64(
        values,
        "_market_position",
        i64::from(position.market_position),
    );
    insert_isize(values, "current_contracts", position.contracts);
    insert_f64(values, "entry_price", position.entry_price);
    insert_f64(values, "avgentryprice", position.avg_entry_price);
    insert_usize(values, "bars_since_entry", position.bars_since_entry);
    insert_f64(values, "exit_price", position.exit_price);
    insert_usize(values, "bars_since_exit", position.bars_since_exit);
    insert_f64(
        values,
        "open_position_profit",
        position.open_position_profit,
    );
    insert_f64(values, "position_profit", position.position_profit);
    insert_f64(values, "net_profit", position.net_profit);
    insert_f64(values, "gross_profit", position.gross_profit);
    insert_f64(values, "gross_loss", position.gross_loss);
    insert_usize(values, "totaltrades", position.total_trades);
    insert_isize(values, "maxcontractsheld", position.max_contracts_held);
    insert_f64(values, "maxiddrawdown", position.max_id_drawdown);
    if emit_gta6 {
        inject_statistics(values, engine, position);
        inject_introspection(values, engine, position);
        if let Some(config) = engine.config() {
            insert_i64(values, "symccy", config.symccy);
            insert_string(values, "symnm", &config.symnm);
            insert_string(values, "symalt", &config.symalt);
            insert_string(values, "descstr", &config.descstr);
            insert_i64(values, "expdate", config.expdate);
        }
    }
}

fn inject_statistics(
    values: &mut BTreeMap<String, Option<String>>,
    engine: &FillEngine,
    position: PositionView,
) {
    let stats = engine.session_stats();
    let total = stats
        .n_win
        .checked_add(stats.n_loss)
        .and_then(|subtotal| subtotal.checked_add(stats.n_even))
        .expect("closed-position statistic count overflow");
    insert_i64(values, "nwin", stats.n_win);
    insert_i64(values, "nlos", stats.n_loss);
    insert_i64(values, "neven", stats.n_even);
    insert_f64(values, "lrgwin", stats.largest_win);
    insert_f64(values, "lrglos", stats.largest_loss);
    insert_f64(
        values,
        "pctprofit",
        if total == 0 {
            0.0
        } else {
            stats.n_win as f64 / total as f64 * 100.0
        },
    );
    insert_i64(values, "maxcwin", stats.max_win_run);
    insert_i64(values, "maxclos", stats.max_loss_run);
    insert_i64(values, "totbwin", stats.total_bars_win);
    insert_i64(values, "totblos", stats.total_bars_loss);
    insert_i64(values, "totbeven", stats.total_bars_even);
    insert_f64(
        values,
        "avgbwin",
        average_bars(stats.total_bars_win, stats.n_win),
    );
    insert_f64(
        values,
        "avgblos",
        average_bars(stats.total_bars_loss, stats.n_loss),
    );
    insert_f64(
        values,
        "avgbeven",
        average_bars(stats.total_bars_even, stats.n_even),
    );

    let aggregates = engine.position_aggregates();
    insert_f64(
        values,
        "maxctrprofit",
        if position.market_position == 0 {
            0.0
        } else {
            aggregates.mfe_pc
        },
    );
    if let Some(last) = engine.closed_positions().last() {
        insert_f64(values, "maxposprofit", last.mfe_pc * last.contracts as f64);
        insert_f64(values, "maxposloss", last.mae_pc * last.contracts as f64);
        insert_isize(values, "maxctr", last.contracts);
        insert_i64(values, "maxentr", last.entries);
    } else {
        insert_f64(values, "maxposprofit", 0.0);
        insert_f64(values, "maxposloss", 0.0);
        insert_isize(values, "maxctr", 0);
        insert_i64(values, "maxentr", 0);
    }
    insert_isize(
        values,
        "maxshr",
        if position.market_position == 0 {
            0
        } else {
            aggregates.max_contracts
        },
    );
    insert_isize(values, "maxshrheld", position.max_contracts_held);
    insert_usize(
        values,
        "maxposago",
        position
            .total_trades
            .checked_add(1)
            .expect("MaxPositionsAgo overflow"),
    );
}

fn inject_introspection(
    values: &mut BTreeMap<String, Option<String>>,
    engine: &FillEngine,
    position: PositionView,
) {
    let in_position = position.market_position != 0;
    let aggregates = engine.position_aggregates();
    insert_i64(
        values,
        "oecount",
        if in_position { aggregates.entries } else { 0 },
    );
    insert_f64(
        values,
        "oeprice",
        if in_position {
            position.entry_price
        } else {
            0.0
        },
    );
    insert_f64(
        values,
        "oeprofit",
        if in_position {
            position.open_position_profit
        } else {
            0.0
        },
    );
    insert_f64(
        values,
        "oemaxprofit",
        if in_position {
            aggregates.mfe_pc * position.contracts as f64
        } else {
            0.0
        },
    );
    insert_f64(
        values,
        "oeminprofit",
        if in_position {
            aggregates.mae_pc * position.contracts as f64
        } else {
            0.0
        },
    );
    insert_i64(
        values,
        "oedate",
        if in_position { engine.entry_date() } else { 0 },
    );
    insert_i64(
        values,
        "oetime",
        if in_position { engine.entry_time() } else { 0 },
    );
    insert_isize(
        values,
        "oecontracts",
        if in_position { position.contracts } else { 0 },
    );
    insert_f64(
        values,
        "oeprofitpc",
        if in_position && position.contracts != 0 {
            position.open_position_profit / position.contracts as f64
        } else {
            0.0
        },
    );
    insert_f64(
        values,
        "oemaxprofitpc",
        if in_position { aggregates.mfe_pc } else { 0.0 },
    );
    insert_f64(
        values,
        "oeminprofitpc",
        if in_position { aggregates.mae_pc } else { 0.0 },
    );
    insert_string(
        values,
        "ennm",
        if in_position { engine.entry_name() } else { "" },
    );
    insert_i64(
        values,
        "endate",
        if in_position { engine.entry_date() } else { 0 },
    );
    insert_i64(
        values,
        "entime",
        if in_position { engine.entry_time() } else { 0 },
    );
    insert_f64(
        values,
        "endtm",
        if in_position {
            el_to_ole(engine.entry_date(), engine.entry_time())
        } else {
            0.0
        },
    );
    insert_i64(
        values,
        "curentr",
        if in_position { aggregates.entries } else { 0 },
    );
    insert_isize(
        values,
        "curshr",
        if in_position { position.contracts } else { 0 },
    );
    insert_i64(values, "execoff", 0);

    if let Some(last) = engine.closed_positions().last() {
        insert_i64(values, "ptcount", last.entries);
        insert_f64(values, "ptprofit", last.pnl);
        insert_isize(values, "ptsize", last.size);
        insert_i64(values, "ptislong", i64::from(last.islong));
        insert_i64(values, "ptisopen", 0);
        insert_f64(values, "ptentrypx", last.entry_px);
        insert_f64(values, "ptexitpx", last.exit_px);
        insert_i64(values, "ptentrybar", last.entry_bar);
        insert_i64(values, "ptexitbar", last.exit_bar);
        insert_f64(
            values,
            "ptentrydtm",
            el_to_ole(last.entry_date, last.entry_time),
        );
        insert_f64(
            values,
            "ptexitdtm",
            el_to_ole(last.exit_date, last.exit_time),
        );
        insert_i64(values, "ptentrycat", last.entry_cat);
        insert_i64(values, "ptexitcat", last.exit_cat);
        insert_string(values, "ptentrynm", &last.entry_name);
        insert_string(values, "ptexitnm", &last.exit_name);
        insert_string(values, "exnm", &last.exit_name);
        insert_i64(values, "exdate", last.exit_date);
        insert_i64(values, "extime", last.exit_time);
        insert_f64(values, "exdtm", el_to_ole(last.exit_date, last.exit_time));
    } else {
        for key in [
            "ptcount",
            "ptprofit",
            "ptsize",
            "ptislong",
            "ptisopen",
            "ptentrypx",
            "ptexitpx",
            "ptentrybar",
            "ptexitbar",
            "ptentrydtm",
            "ptexitdtm",
            "ptentrycat",
            "ptexitcat",
            "exdate",
            "extime",
            "exdtm",
        ] {
            insert_i64(values, key, 0);
        }
        insert_string(values, "ptentrynm", "");
        insert_string(values, "ptexitnm", "");
        insert_string(values, "exnm", "");
    }
}

fn average_bars(total: i64, count: i64) -> f64 {
    if count == 0 {
        0.0
    } else {
        total as f64 / count as f64
    }
}

fn insert_f64(values: &mut BTreeMap<String, Option<String>>, key: &str, value: f64) {
    values.insert(key.to_owned(), Some(python_float_string(value)));
}

fn insert_i64(values: &mut BTreeMap<String, Option<String>>, key: &str, value: i64) {
    values.insert(key.to_owned(), Some(value.to_string()));
}

fn insert_isize(values: &mut BTreeMap<String, Option<String>>, key: &str, value: isize) {
    values.insert(key.to_owned(), Some(value.to_string()));
}

fn insert_usize(values: &mut BTreeMap<String, Option<String>>, key: &str, value: usize) {
    values.insert(key.to_owned(), Some(value.to_string()));
}

fn insert_string(values: &mut BTreeMap<String, Option<String>>, key: &str, value: &str) {
    values.insert(key.to_owned(), Some(value.to_owned()));
}

/// Render the ordinary finite values emitted by the engine like Python
/// `str(float)`, including Python's fixed/scientific thresholds and exponent
/// padding. Rust and Python use the same shortest-round-trip digits; their
/// presentation rules differ.
fn python_float_string(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value == f64::INFINITY {
        return "inf".to_owned();
    }
    if value == f64::NEG_INFINITY {
        return "-inf".to_owned();
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0".to_owned()
        } else {
            "0.0".to_owned()
        };
    }

    let raw = format!("{value:?}");
    let (negative, unsigned) = raw
        .strip_prefix('-')
        .map_or((false, raw.as_str()), |rest| (true, rest));
    let (mantissa, explicit_exponent) = unsigned
        .split_once(['e', 'E'])
        .map_or((unsigned, None), |(mantissa, exponent)| {
            (mantissa, exponent.parse::<i32>().ok())
        });
    let mut digits: String = mantissa.chars().filter(|ch| *ch != '.').collect();
    let decimal_exponent = if let Some(exponent) = explicit_exponent {
        exponent
    } else if let Some(dot) = mantissa.find('.') {
        let before = &mantissa[..dot];
        if before != "0" {
            before.len() as i32 - 1
        } else {
            let zeros = mantissa[dot + 1..]
                .chars()
                .take_while(|ch| *ch == '0')
                .count();
            -(zeros as i32) - 1
        }
    } else {
        mantissa.len() as i32 - 1
    };
    while digits.starts_with('0') {
        digits.remove(0);
    }
    while digits.ends_with('0') {
        digits.pop();
    }
    if digits.is_empty() {
        digits.push('0');
    }

    let mut rendered = if (-4..16).contains(&decimal_exponent) {
        let decimal_place = decimal_exponent + 1;
        if decimal_place <= 0 {
            format!("0.{}{}", "0".repeat((-decimal_place) as usize), digits)
        } else if decimal_place as usize >= digits.len() {
            format!(
                "{}{}.0",
                digits,
                "0".repeat(decimal_place as usize - digits.len())
            )
        } else {
            let split = decimal_place as usize;
            format!("{}.{}", &digits[..split], &digits[split..])
        }
    } else {
        let fraction = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let sign = if decimal_exponent >= 0 { '+' } else { '-' };
        format!(
            "{}{}e{}{:02}",
            &digits[..1],
            fraction,
            sign,
            decimal_exponent.abs()
        )
    };
    if negative {
        rendered.insert(0, '-');
    }
    rendered
}

fn el_to_ole(el_date: i64, el_time: i64) -> f64 {
    if el_date <= 0 {
        return 0.0;
    }
    let year = 1900 + el_date / 10_000;
    let month = (el_date / 100) % 100;
    let day = el_date % 100;
    if !valid_date(year, month, day) {
        return 0.0;
    }
    let hour = el_time / 100;
    let minute = el_time % 100;
    if !(0..24).contains(&hour) || !(0..60).contains(&minute) {
        return 0.0;
    }
    let days = days_from_civil(year, month, day) + 25_569;
    days as f64 + (hour * 3_600 + minute * 60) as f64 / 86_400.0
}

fn valid_date(year: i64, month: i64, day: i64) -> bool {
    if !(1..=12).contains(&month) || day < 1 {
        return false;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    day <= days
}

// Days since 1970-01-01, for a validated proleptic-Gregorian date.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = year - i64::from(month <= 2);
    let era = if adjusted_year >= 0 {
        adjusted_year
    } else {
        adjusted_year - 399
    } / 400;
    let year_of_era = adjusted_year - era * 400;
    let shifted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

#[cfg(test)]
mod tests {
    use super::{el_to_ole, python_float_string};

    #[test]
    fn float_rendering_uses_python_presentation() {
        let cases = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (12.25, "12.25"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (-1e-7, "-1e-07"),
        ];
        for (value, expected) in cases {
            assert_eq!(python_float_string(value), expected, "{value:?}");
        }
    }

    #[test]
    fn ole_conversion_matches_known_epoch_and_fraction() {
        assert_eq!(el_to_ole(1000101, 0), 36_526.0);
        assert_eq!(el_to_ole(1240102, 1200), 45_293.5);
        assert_eq!(el_to_ole(0, 0), 0.0);
        assert_eq!(el_to_ole(1240230, 1200), 0.0);
    }
}
