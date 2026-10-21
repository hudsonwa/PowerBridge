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

use crate::types::{
    Action, Bar, BarTape, Config, Instrument, KernelError, Order, OrderType, PositionView,
    RiskUpdate, Session, SessionEngine, StrategyOutput, Timing,
};

use super::{
    run_configured_with_trace, run_native, run_native_window, run_native_window_with_trace,
    run_native_window_with_trace_sink, run_native_with_trace, run_native_with_trace_sink,
    FillEngine,
};

fn bar(open: f64, high: f64, low: f64, close: f64, date: i64, time: i64) -> Bar {
    Bar {
        open,
        high,
        low,
        close,
        volume: 1.0,
        date,
        time,
    }
}

fn tape(bars: &[Bar]) -> BarTape {
    BarTape::from_columns(
        bars.iter().map(|item| item.open).collect(),
        bars.iter().map(|item| item.high).collect(),
        bars.iter().map(|item| item.low).collect(),
        bars.iter().map(|item| item.close).collect(),
        bars.iter().map(|item| item.volume).collect(),
        bars.iter().map(|item| item.date).collect(),
        bars.iter().map(|item| item.time).collect(),
    )
    .unwrap()
}

fn config() -> Config {
    Config {
        name: "NQ".to_owned(),
        bigpointvalue: 20.0,
        pointvalue: 5.0,
        minmove: 1,
        pricescale: 4,
        tick: 0.25,
        barinterval: 1,
        session: Session {
            start: 830,
            end: 1515,
        },
        commission: 0.0,
        slippage: 0.0,
        max_bars_back: 50,
        max_entries_per_direction: 0,
        warmup_seed: 0,
        symccy: 840,
        symnm: "NQ".to_owned(),
        symalt: "NQ".to_owned(),
        descstr: "E-mini NASDAQ-100".to_owned(),
        expdate: 0,
    }
}

fn queue(
    engine: &mut FillEngine,
    orders: Vec<Order>,
    risk: RiskUpdate,
    current_bar: Bar,
    bar_number: usize,
) {
    engine
        .after_bar(StrategyOutput { orders, risk }, current_bar, bar_number)
        .unwrap();
}

#[test]
fn explicit_close_records_full_position_context() {
    let bars = tape(&[
        bar(99.0, 100.0, 98.0, 99.0, 1240102, 930),
        bar(100.0, 102.0, 99.0, 101.0, 1240102, 931),
        bar(105.0, 106.0, 104.0, 105.0, 1240102, 932),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, position: PositionView| {
        let orders = match bar_number {
            1 => vec![Order::market(Action::Buy, 2.0, "LE")],
            2 if position.market_position == 1 => {
                vec![Order::market(Action::Sell, 0.0, "LX")]
            }
            _ => Vec::new(),
        };
        Ok(StrategyOutput {
            orders,
            risk: RiskUpdate::default(),
        })
    };

    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert_eq!(outcome.fills.len(), 4);
    assert_eq!(outcome.closed_positions.len(), 1);
    let closed = &outcome.closed_positions[0];
    assert_eq!(closed.pnl, 200.0);
    assert_eq!(closed.bars, 2);
    assert_eq!(closed.contracts, 2);
    assert_eq!(closed.entries, 1);
    assert_eq!(closed.size, 2);
    assert!(closed.islong);
    assert_eq!(closed.entry_px, 100.0);
    assert_eq!(closed.exit_px, 105.0);
    assert_eq!(closed.entry_bar, 2);
    assert_eq!(closed.exit_bar, 3);
    assert_eq!(closed.entry_date, 1240102);
    assert_eq!(closed.entry_time, 931);
    assert_eq!(closed.exit_date, 1240102);
    assert_eq!(closed.exit_time, 932);
    assert_eq!(closed.entry_name, "LE");
    assert_eq!(closed.exit_name, "LX");
    assert_eq!(closed.entry_cat, 3);
    assert_eq!(closed.exit_cat, 3);
}

#[test]
fn scale_out_keeps_one_ledger_position_and_running_aggregates() {
    let mut engine = FillEngine::new(Instrument::default());
    let bars = [
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(100.0, 101.0, 99.0, 100.0, 1240102, 931),
        bar(100.0, 102.0, 99.0, 101.0, 1240102, 932),
        bar(103.0, 103.0, 102.0, 103.0, 1240102, 933),
        bar(104.0, 105.0, 103.0, 104.0, 1240102, 934),
    ];

    queue(
        &mut engine,
        vec![Order::market(Action::Buy, 2.0, "LE1")],
        RiskUpdate::default(),
        bars[0],
        1,
    );
    engine.before_bar(bars[1], 2).unwrap();
    queue(
        &mut engine,
        vec![Order::market(Action::Buy, 1.0, "LE2")],
        RiskUpdate::default(),
        bars[1],
        2,
    );
    engine.before_bar(bars[2], 3).unwrap();
    queue(
        &mut engine,
        vec![Order::market(Action::Sell, 1.0, "LX1")],
        RiskUpdate::default(),
        bars[2],
        3,
    );
    engine.before_bar(bars[3], 4).unwrap();
    assert!(engine.closed_positions().is_empty());
    queue(
        &mut engine,
        vec![Order::market(Action::Sell, 0.0, "LX2")],
        RiskUpdate::default(),
        bars[3],
        4,
    );
    engine.before_bar(bars[4], 5).unwrap();

    assert_eq!(engine.fills().len(), 6);
    assert_eq!(engine.closed_positions().len(), 1);
    let closed = &engine.closed_positions()[0];
    assert_eq!(closed.pnl, 220.0);
    assert_eq!(closed.bars, 4);
    assert_eq!(closed.contracts, 3);
    assert_eq!(closed.entries, 2);
    assert_eq!(closed.size, 2);
    assert_eq!(closed.mfe_pc, 80.0);
    assert_eq!(closed.mae_pc, -20.0);
    assert_eq!(closed.entry_name, "LE1");
    assert_eq!(closed.exit_name, "LX2");
    let stats = engine.session_stats();
    assert_eq!(stats.n_win, 1);
    assert_eq!(stats.n_loss, 0);
    assert_eq!(stats.n_even, 0);
    assert_eq!(stats.largest_win, 220.0);
    assert_eq!(stats.total_bars_win, 4);
    assert_eq!(stats.max_win_run, 1);
}

#[test]
fn exit_price_is_folded_into_closed_excursion() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(100.0, 101.0, 99.0, 100.0, 1240102, 931),
        bar(90.0, 90.0, 90.0, 90.0, 1240102, 932),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        Ok(StrategyOutput {
            orders: match bar_number {
                1 => vec![Order::market(Action::Buy, 1.0, "LE")],
                2 => vec![Order::market(Action::Sell, 0.0, "LX")],
                _ => Vec::new(),
            },
            risk: RiskUpdate::default(),
        })
    };

    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    let closed = &outcome.closed_positions[0];
    assert_eq!(closed.mfe_pc, 20.0);
    assert_eq!(closed.mae_pc, -200.0);
}

#[test]
fn session_rollover_records_end_of_day_metadata() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 1514),
        bar(100.0, 105.0, 100.0, 105.0, 1240102, 1515),
        bar(110.0, 110.0, 110.0, 110.0, 1240103, 831),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        Ok(StrategyOutput {
            orders: if bar_number == 1 {
                vec![Order::market(Action::Buy, 1.0, "LE")]
            } else {
                Vec::new()
            },
            risk: RiskUpdate {
                exit_on_close: Some(true),
                ..RiskUpdate::default()
            },
        })
    };

    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert_eq!(outcome.position.market_position, 0);
    assert_eq!(outcome.closed_positions.len(), 1);
    let closed = &outcome.closed_positions[0];
    assert_eq!(closed.pnl, 100.0);
    assert_eq!(closed.exit_bar, 2);
    assert_eq!(closed.exit_date, 1240102);
    assert_eq!(closed.exit_time, 1515);
    assert_eq!(closed.exit_name, "End of Day Exit");
    assert_eq!(closed.exit_cat, 4);
}

#[test]
fn on_close_reversal_precedes_session_flatten() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 1514),
        bar(100.0, 105.0, 95.0, 102.0, 1240102, 1515),
        bar(110.0, 110.0, 110.0, 110.0, 1240103, 831),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        let orders = if bar_number == 1 {
            vec![Order::market(Action::Buy, 1.0, "LE")]
        } else if bar_number == 2 {
            let mut reversal = Order::market(Action::SellShort, 3.0, "SE_CLOSE");
            reversal.order_type = OrderType::Close;
            reversal.timing = Timing::ThisBar;
            vec![reversal]
        } else {
            Vec::new()
        };
        Ok(StrategyOutput {
            orders,
            risk: RiskUpdate {
                exit_on_close: Some(true),
                ..RiskUpdate::default()
            },
        })
    };

    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert_eq!(outcome.position.market_position, 0);
    assert_eq!(outcome.total_trades, 2);
    assert_eq!(outcome.closed_positions.len(), 2);
    assert!(outcome.closed_positions[0].islong);
    assert!(!outcome.closed_positions[1].islong);
    assert_eq!(outcome.closed_positions[0].exit_name, "SE_CLOSE");
    assert_eq!(outcome.closed_positions[1].exit_name, "End of Day Exit");
}

#[test]
fn report_uses_first_sorted_columns_and_zero_for_missing_values() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(101.0, 101.0, 101.0, 101.0, 1240102, 931),
    ]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let (outcome, report) = run_native_with_trace(
        &bars,
        Instrument::default(),
        &mut strategy,
        false,
        |_strategy, _bar, bar_number, _position| {
            let mut values = BTreeMap::new();
            if bar_number == 1 {
                values.insert("z_strategy".to_owned(), Some("seen".to_owned()));
                values.insert("missing_strategy".to_owned(), None);
            } else {
                values.insert("later_only".to_owned(), Some("ignored".to_owned()));
            }
            Ok(values)
        },
    )
    .unwrap();

    assert_eq!(outcome.fills.len(), 0);
    assert_eq!(report.rows.len(), 2);
    let mut sorted = report.columns.clone();
    sorted.sort();
    assert_eq!(report.columns, sorted);
    assert!(!report.columns.iter().any(|name| name == "later_only"));
    assert!(!report.columns.iter().any(|name| name == "nwin"));
    assert!(!report.columns.iter().any(|name| name == "oecount"));
    let missing = report
        .columns
        .iter()
        .position(|name| name == "missing_strategy")
        .unwrap();
    let strategy_value = report
        .columns
        .iter()
        .position(|name| name == "z_strategy")
        .unwrap();
    assert_eq!(report.rows[0][missing], "0");
    assert_eq!(report.rows[0][strategy_value], "seen");
    assert_eq!(report.rows[1][strategy_value], "0");
}

#[test]
fn configured_report_includes_explicit_instrument_fields() {
    let bars = tape(&[bar(100.0, 100.0, 100.0, 100.0, 1240102, 930)]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let (_, report) =
        run_configured_with_trace(&bars, &config(), &mut strategy, true, |_, _, _, _| {
            Ok(BTreeMap::new())
        })
        .unwrap();
    let cell = |column: &str| {
        let index = report
            .columns
            .iter()
            .position(|name| name == column)
            .unwrap();
        report.rows[0][index].clone()
    };
    assert_eq!(cell("symccy"), "840");
    assert_eq!(cell("symnm"), "NQ");
    assert_eq!(cell("symalt"), "NQ");
    assert_eq!(cell("descstr"), "E-mini NASDAQ-100");
    assert_eq!(cell("expdate"), "0");
}

#[test]
fn configured_report_can_disable_gta6_injection() {
    let bars = tape(&[bar(100.0, 100.0, 100.0, 100.0, 1240102, 930)]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let (_, report) =
        run_configured_with_trace(&bars, &config(), &mut strategy, false, |_, _, _, _| {
            Ok(BTreeMap::new())
        })
        .unwrap();

    assert!(report.columns.iter().any(|name| name == "net_profit"));
    assert!(!report.columns.iter().any(|name| name == "nwin"));
    assert!(!report.columns.iter().any(|name| name == "oecount"));
    assert!(!report.columns.iter().any(|name| name == "symnm"));
}

#[test]
fn session_statistics_classify_win_loss_and_even_positions() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 931),
        bar(110.0, 110.0, 110.0, 110.0, 1240102, 932),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 933),
        bar(90.0, 90.0, 90.0, 90.0, 1240102, 934),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 935),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 936),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        let action = if bar_number % 2 == 1 {
            Action::Buy
        } else {
            Action::Sell
        };
        Ok(StrategyOutput {
            orders: vec![Order::market(action, 0.0, "STAT")],
            risk: RiskUpdate::default(),
        })
    };
    let (outcome, report) = run_native_with_trace(
        &bars,
        Instrument::default(),
        &mut strategy,
        true,
        |_, _, _, _| Ok(BTreeMap::new()),
    )
    .unwrap();

    assert_eq!(outcome.closed_positions.len(), 3);
    let cell = |column: &str| {
        let index = report
            .columns
            .iter()
            .position(|name| name == column)
            .unwrap();
        report.rows.last().unwrap()[index].clone()
    };
    assert_eq!(cell("nwin"), "1");
    assert_eq!(cell("nlos"), "1");
    assert_eq!(cell("neven"), "1");
    assert_eq!(cell("totbwin"), "2");
    assert_eq!(cell("totblos"), "2");
    assert_eq!(cell("totbeven"), "2");
    assert_eq!(cell("maxcwin"), "1");
    assert_eq!(cell("maxclos"), "1");
}

#[test]
fn seeded_window_runs_prefix_state_and_queues_only_last_warmup_bar() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(101.0, 101.0, 101.0, 101.0, 1240102, 931),
        bar(102.0, 102.0, 102.0, 102.0, 1240102, 932),
    ]);
    let mut calls = 0;
    let mut strategy = |_bar: Bar, _bar_number: usize, _position: PositionView| {
        calls += 1;
        Ok(StrategyOutput {
            orders: vec![Order::market(Action::Buy, 1.0, "WINDOW")],
            risk: RiskUpdate::default(),
        })
    };
    let (outcome, report) = run_native_window_with_trace(
        &bars,
        Instrument::default(),
        &mut strategy,
        2,
        0,
        false,
        |_, _, _, _| Ok(BTreeMap::new()),
    )
    .unwrap();

    assert_eq!(calls, 3);
    assert_eq!(report.rows.len(), 1);
    assert_eq!(outcome.fills.len(), 1);
    assert_eq!(outcome.fills[0].date, 1240102);
    assert_eq!(outcome.fills[0].time, 932);
    assert_eq!(outcome.position.contracts, 1);
}

#[test]
fn seeded_prefix_stop_is_in_force_when_the_window_opens() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 931),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 932),
        bar(100.0, 100.0, 90.0, 90.0, 1240102, 933),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| match bar_number {
        1 => Ok(StrategyOutput {
            orders: Vec::new(),
            risk: RiskUpdate {
                stop_loss: Some(100.0),
                ..RiskUpdate::default()
            },
        }),
        2 => Ok(StrategyOutput {
            orders: vec![Order::market(Action::Buy, 1.0, "LE")],
            risk: RiskUpdate::default(),
        }),
        _ => Ok(StrategyOutput::default()),
    };

    let outcome = run_native_window(&bars, Instrument::default(), &mut strategy, 2).unwrap();
    assert_eq!(outcome.position.market_position, 0);
    assert_eq!(outcome.closed_positions.len(), 1);
    assert_eq!(outcome.fills.len(), 2);
    assert_eq!(outcome.fills[1].signal, "stop_loss");
}

#[test]
fn reversal_and_cover_log_every_position_change() {
    // Buy, SellShort (long 1 -> short 1), BuyToCover: the reversal logs its
    // close leg and its open leg, so four records with the order sides.
    let flat = |time| bar(100.0, 100.0, 100.0, 100.0, 1240102, time);
    let bars = tape(&[flat(930), flat(931), flat(932), flat(933), flat(934)]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        let order = match bar_number {
            1 => Order::market(Action::Buy, 1.0, "LE"),
            2 => Order::market(Action::SellShort, 1.0, "SE"),
            3 => Order::market(Action::BuyToCover, 1.0, "SX"),
            _ => return Ok(StrategyOutput::default()),
        };
        Ok(StrategyOutput {
            orders: vec![order],
            risk: RiskUpdate::default(),
        })
    };

    let outcome = run_native_window(&bars, Instrument::default(), &mut strategy, 0).unwrap();
    assert_eq!(outcome.position.market_position, 0);
    let got: Vec<(&str, &str, i64)> = outcome
        .fills
        .iter()
        .map(|fill| (fill.fill_type, fill.signal.as_str(), fill.time))
        .collect();
    assert_eq!(
        got,
        [
            ("EntryLong", "LE", 931),
            ("ExitLong", "SE", 932),
            ("ExitLong", "SE", 932),
            ("EntryLong", "SX", 933),
        ]
    );
}

#[test]
fn signed_negative_quantity_survives_to_position_state() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 931),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        Ok(StrategyOutput {
            orders: if bar_number == 1 {
                vec![Order::market(Action::Buy, -3.0, "LE_NEG")]
            } else {
                Vec::new()
            },
            risk: RiskUpdate::default(),
        })
    };

    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert_eq!(outcome.position.market_position, 1);
    assert_eq!(outcome.position.contracts, -3);
    assert_eq!(outcome.fills.len(), 3);
}

#[test]
fn conditional_order_arbitration_is_low_first_on_tie() {
    let mut engine = FillEngine::new(Instrument::default());
    let first = bar(100.0, 100.0, 100.0, 100.0, 1240102, 930);
    queue(
        &mut engine,
        vec![Order::market(Action::Buy, 1.0, "LE")],
        RiskUpdate::default(),
        first,
        1,
    );
    engine
        .before_bar(bar(100.0, 100.0, 100.0, 100.0, 1240102, 931), 2)
        .unwrap();
    queue(
        &mut engine,
        vec![
            Order::conditional(Action::Sell, OrderType::Limit, 0.0, "PT", 105.0),
            Order::conditional(Action::Sell, OrderType::Stop, 0.0, "SL", 95.0),
        ],
        RiskUpdate::default(),
        first,
        2,
    );
    engine
        .before_bar(bar(100.0, 106.0, 94.0, 100.0, 1240102, 932), 3)
        .unwrap();

    assert_eq!(engine.fills().len(), 2);
    assert_eq!(engine.fills()[1].signal, "SL");
    assert_eq!(engine.closed_positions().len(), 1);
    assert_eq!(engine.closed_positions()[0].pnl, -100.0);
}

#[test]
fn conditional_fill_price_keeps_python_nan_and_signed_zero_ordering() {
    let signal = bar(1.0, 1.0, 1.0, 1.0, 1240102, 930);

    let mut nan_engine = FillEngine::new(Instrument::default());
    queue(
        &mut nan_engine,
        vec![Order::conditional(
            Action::Buy,
            OrderType::Limit,
            1.0,
            "NAN_OPEN",
            100.0,
        )],
        RiskUpdate::default(),
        signal,
        1,
    );
    nan_engine
        .before_bar(bar(f64::NAN, 101.0, 99.0, 100.0, 1240102, 931), 2)
        .unwrap();
    assert!(nan_engine.fills()[0].price.is_nan());

    let mut zero_engine = FillEngine::new(Instrument::default());
    queue(
        &mut zero_engine,
        vec![Order::conditional(
            Action::Buy,
            OrderType::Limit,
            1.0,
            "SIGNED_ZERO",
            -0.0,
        )],
        RiskUpdate::default(),
        signal,
        1,
    );
    zero_engine
        .before_bar(bar(0.0, 1.0, -1.0, 0.0, 1240102, 931), 2)
        .unwrap();
    assert_eq!(zero_engine.fills()[0].price, 0.0);
    assert!(!zero_engine.fills()[0].price.is_sign_negative());
}

#[test]
fn non_finite_tick_snap_fails_loud() {
    let mut engine = FillEngine::new(Instrument {
        point_value: 20.0,
        tick: f64::NAN,
        max_entries_per_direction: 0,
    });
    queue(
        &mut engine,
        vec![Order::market(Action::Buy, 1.0, "LE")],
        RiskUpdate {
            stop_loss: Some(100.0),
            ..RiskUpdate::default()
        },
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        1,
    );
    let error = engine
        .before_bar(bar(100.0, 101.0, 99.0, 100.0, 1240102, 931), 2)
        .unwrap_err();
    assert_eq!(
        error,
        KernelError::unsupported("non-finite money-management tick quotient NaN")
    );
}

#[test]
fn this_bar_close_is_visible_on_the_following_bar() {
    let bars = tape(&[
        bar(100.0, 101.0, 99.0, 100.25, 1240102, 930),
        bar(110.0, 110.0, 110.0, 110.0, 1240102, 931),
    ]);
    let mut seen = Vec::new();
    let mut strategy = |_: Bar, bar_number: usize, position: PositionView| {
        seen.push((bar_number, position.market_position));
        let mut order = Order::market(Action::Buy, 1.0, "LE_CLOSE");
        order.order_type = OrderType::Close;
        order.timing = Timing::ThisBar;
        Ok(StrategyOutput {
            orders: if bar_number == 1 {
                vec![order]
            } else {
                Vec::new()
            },
            risk: RiskUpdate::default(),
        })
    };

    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert_eq!(seen, vec![(1, 0), (2, 1)]);
    assert_eq!(outcome.fills[0].date, 1240102);
    assert_eq!(outcome.fills[0].time, 930);
    assert_eq!(outcome.fills[0].price, 100.25);
}

#[test]
fn non_finite_quantity_fails_loud_at_fill_time() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 931),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, _position: PositionView| {
        Ok(StrategyOutput {
            orders: if bar_number == 1 {
                vec![Order::market(Action::Buy, f64::NAN, "BAD")]
            } else {
                Vec::new()
            },
            risk: RiskUpdate::default(),
        })
    };

    let error = run_native(&bars, Instrument::default(), &mut strategy).unwrap_err();
    assert_eq!(
        error,
        KernelError::unsupported("non-finite order quantity NaN")
    );
}

#[test]
fn trace_sink_without_retention_receives_every_row() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(101.0, 101.0, 101.0, 101.0, 1240102, 931),
        bar(102.0, 102.0, 102.0, 102.0, 1240102, 932),
    ]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let mut sunk = Vec::new();
    let mut headers = Vec::new();
    let mut row_sink = |row: &[String]| sunk.push(row.to_vec());
    let mut header_sink = |columns: &[String]| headers.push(columns.to_vec());
    let (outcome, report) = run_native_with_trace_sink(
        &bars,
        Instrument::default(),
        &mut strategy,
        false,
        |_strategy, _bar, bar_number, _position| {
            let mut values = BTreeMap::new();
            values.insert("bn".to_owned(), Some(bar_number.to_string()));
            Ok(values)
        },
        Some(&mut row_sink),
        Some(&mut header_sink),
        None,
    )
    .unwrap();

    assert_eq!(outcome.fills.len(), 0);
    assert!(report.rows.is_empty());
    assert_eq!(sunk.len(), 3);
    assert_eq!(headers.len(), 1);
    assert_eq!(headers[0], report.columns);
    assert!(!report.columns.is_empty());
    let bn = report.columns.iter().position(|name| name == "bn").unwrap();
    assert_eq!(sunk[0][bn], "1");
    assert_eq!(sunk[1][bn], "2");
    assert_eq!(sunk[2][bn], "3");
}

#[test]
fn trace_sink_and_retain_keeps_rows() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(101.0, 101.0, 101.0, 101.0, 1240102, 931),
    ]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let mut sunk = Vec::new();
    let mut row_sink = |row: &[String]| sunk.push(row.to_vec());
    let (_, report) = run_native_with_trace_sink(
        &bars,
        Instrument::default(),
        &mut strategy,
        false,
        |_, _, _, _| Ok(BTreeMap::new()),
        Some(&mut row_sink),
        None,
        Some(true),
    )
    .unwrap();

    assert_eq!(sunk.len(), 2);
    assert_eq!(report.rows.len(), 2);
    assert_eq!(sunk, report.rows);
}

#[test]
fn trace_sink_window_still_suppresses_warmup_rows() {
    let bars = tape(&[
        bar(100.0, 100.0, 100.0, 100.0, 1240102, 930),
        bar(101.0, 101.0, 101.0, 101.0, 1240102, 931),
        bar(102.0, 102.0, 102.0, 102.0, 1240102, 932),
        bar(103.0, 103.0, 103.0, 103.0, 1240102, 933),
    ]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let mut sunk = Vec::new();
    let mut row_sink = |row: &[String]| sunk.push(row.to_vec());
    let (_, report) = run_native_window_with_trace_sink(
        &bars,
        Instrument::default(),
        &mut strategy,
        0,
        2,
        false,
        |_strategy, _bar, bar_number, _position| {
            let mut values = BTreeMap::new();
            values.insert("bn".to_owned(), Some(bar_number.to_string()));
            Ok(values)
        },
        Some(&mut row_sink),
        None,
        None,
    )
    .unwrap();

    assert!(report.rows.is_empty());
    assert_eq!(sunk.len(), 2);
    let bn = report.columns.iter().position(|name| name == "bn").unwrap();
    assert_eq!(sunk[0][bn], "3");
    assert_eq!(sunk[1][bn], "4");

    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let (_, retained) = run_native_window_with_trace(
        &bars,
        Instrument::default(),
        &mut strategy,
        0,
        2,
        false,
        |_strategy, _bar, bar_number, _position| {
            let mut values = BTreeMap::new();
            values.insert("bn".to_owned(), Some(bar_number.to_string()));
            Ok(values)
        },
    )
    .unwrap();
    assert_eq!(retained.rows.len(), 2);
    let bn = retained
        .columns
        .iter()
        .position(|name| name == "bn")
        .unwrap();
    assert_eq!(retained.rows[0][bn], "3");
    assert_eq!(retained.rows[1][bn], "4");
}

#[test]
fn no_work_state_flips_on_position_orders_and_risk() {
    let flat_bar = bar(100.0, 101.0, 99.0, 100.0, 1240102, 930);

    // Fresh engine is no-work.
    let mut engine = FillEngine::from_config(&config());
    assert!(engine.no_work_state());

    // A queued next-bar order flips it out.
    queue(
        &mut engine,
        vec![Order::market(Action::Buy, 1.0, "LE")],
        RiskUpdate::default(),
        flat_bar,
        1,
    );
    assert!(!engine.no_work_state());

    // An open position (from a fill) is not no-work.
    let mut positioned = FillEngine::from_config(&config());
    queue(
        &mut positioned,
        vec![Order::market(Action::Buy, 1.0, "LE")],
        RiskUpdate::default(),
        flat_bar,
        1,
    );
    positioned.before_bar(flat_bar, 2).unwrap();
    assert!(!positioned.no_work_state());

    // A queued on-close order flips it out.
    let mut onclose = FillEngine::from_config(&config());
    let mut close = Order::market(Action::Buy, 1.0, "LE_CLS");
    close.order_type = OrderType::Close;
    close.timing = Timing::ThisBar;
    queue(
        &mut onclose,
        vec![close],
        RiskUpdate::default(),
        flat_bar,
        1,
    );
    assert!(!onclose.no_work_state());

    // Armed risk flips it out.
    let mut risked = FillEngine::from_config(&config());
    risked.apply_risk(RiskUpdate {
        stop_loss: Some(500.0),
        ..RiskUpdate::default()
    });
    assert!(!risked.no_work_state());
}

#[test]
fn no_work_fastpath_counts_all_flat_bars() {
    let bars = tape(&[
        bar(100.0, 101.0, 99.0, 100.5, 1240102, 930),
        bar(100.5, 101.5, 99.5, 101.0, 1240102, 931),
        bar(101.0, 102.0, 100.0, 101.5, 1240102, 932),
    ]);
    let mut strategy =
        |_bar: Bar, _bar_number: usize, _position: PositionView| Ok(StrategyOutput::default());
    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert_eq!(outcome.fast_path_hits, 3);
    assert_eq!(outcome.fills.len(), 0);
    assert_eq!(outcome.max_id_drawdown, 0.0);
}

#[test]
fn no_work_transition_entry_exit_counts_real_skips() {
    let bars = tape(&[
        bar(100.0, 101.0, 99.0, 100.5, 1240102, 930),
        bar(100.5, 101.5, 99.5, 101.0, 1240102, 931),
        bar(101.0, 102.0, 100.0, 101.5, 1240102, 932),
        bar(101.5, 102.5, 100.5, 102.0, 1240102, 933),
        bar(102.0, 103.0, 101.0, 102.5, 1240102, 934),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, position: PositionView| {
        let orders = match bar_number {
            1 => vec![Order::market(Action::Buy, 1.0, "LE")],
            2 if position.market_position == 1 => {
                vec![Order::market(Action::Sell, 0.0, "LX")]
            }
            _ => Vec::new(),
        };
        Ok(StrategyOutput {
            orders,
            risk: RiskUpdate::default(),
        })
    };
    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    // bar 0 (flat, no order) and bars 3-4 (flat after the exit) are no-work.
    assert_eq!(outcome.fast_path_hits, 3);
    assert_eq!(outcome.fills.len(), 2);
    assert_eq!(outcome.position.market_position, 0);
}

#[test]
fn no_work_drawdown_survives_losing_round_trip() {
    // A long entered high and closed lower folds the realized loss into
    // max_id_drawdown on the exit bar and keeps it across flat no-work bars.
    let bars = tape(&[
        bar(200.0, 201.0, 199.0, 200.5, 1240102, 930),
        bar(199.0, 200.0, 198.0, 199.5, 1240102, 931),
        bar(198.0, 199.0, 197.0, 198.5, 1240102, 932),
        bar(197.0, 198.0, 196.0, 197.5, 1240102, 933),
        bar(196.0, 197.0, 195.0, 196.5, 1240102, 934),
    ]);
    let mut strategy = |_bar: Bar, bar_number: usize, position: PositionView| {
        let orders = match bar_number {
            1 => vec![Order::market(Action::Buy, 1.0, "LE")],
            2 if position.market_position == 1 => {
                vec![Order::market(Action::Sell, 0.0, "LX")]
            }
            _ => Vec::new(),
        };
        Ok(StrategyOutput {
            orders,
            risk: RiskUpdate::default(),
        })
    };
    let outcome = run_native(&bars, Instrument::default(), &mut strategy).unwrap();
    assert!(outcome.max_id_drawdown < 0.0, "{}", outcome.max_id_drawdown);
    assert_eq!(outcome.fast_path_hits, 3);
    assert_eq!(outcome.fills.len(), 2);
}
