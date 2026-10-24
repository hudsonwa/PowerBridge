// Copyright 2026 Joshua Hudson
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// See the License for the specific language governing permissions
// and limitations under the License.

//! SPIKE: typed `audit-event-v1` packet collected on the production kernel loop.
//!
//! This is a **new entry point**, not a change to the stable `run_program`
//! contract or to the typed trace. Numeric values stay typed
//! across the FFI; Python owns legacy text rendering.

use std::collections::VecDeque;

use sha2::{Digest, Sha256};

use crate::types::{Bar, BarTape, Config, Fill, KernelError, KernelOutcome, PositionView};

/// Magic for the 256-byte little-endian header (`AUEVT1` + two NUL pads).
pub(crate) const MAGIC: &[u8; 8] = b"AUEVT1\0\0";
pub(crate) const VERSION: u16 = 1;
pub(crate) const HEADER_SIZE: usize = 256;
/// Schema item 6 B: drop `priorsessionweightedmean` and
/// `c3entrylevel` (assigned in EL, never read, `dce_dead_stores` zeroes
/// them; they are not inputs to `trades.csv`). 120 B -> 104 B.
pub(crate) const TRANSITION_SIZE: usize = 104;
pub(crate) const TRADE_SIZE: usize = 88;

/// Interpreter slots projected by stable (lowercased) IR name.
///
/// Engine-owned columns (`current_contracts`, `entry_price`, `net_profit`,
/// `totaltrades`, `_market_position`) are read from [`PositionView`], not here.
///
/// Schema item 6 B deliberately omits `priorsessionweightedmean` and
/// `c3entrylevel`: `dce_dead_stores` drops their `StoreVar`, they
/// are never read by later strategy logic, and they are not inputs to
/// `trades.csv`. A formatter that needs either name must raise, not default.
#[allow(dead_code)] // named in tests and is the schema checklist for SlotMap::bind
pub(crate) const NQ_AUDIT_SLOT_NAMES: [&str; 14] = [
    "priorsessionclose",
    "priorsessionunadjustedclose",
    "priorsessiontr",
    "c3scaledlevel",
    "c4cappedrange",
    "c4traildistance",
    "historyready",
    "c1gate",
    "entrygate",
    "ladderbarselapsed",
    "bestclosesincesignal",
    "trailstop",
    "emergencyexit",
    "eodexit",
];

const KIND_SCALE_IN: u8 = 1;
const KIND_FULL_EXIT: u8 = 2;
const DIR_LONG: u8 = 1;

const FLAG_HASH_DATA1: u32 = 1 << 0;
const FLAG_HASH_DATA2: u32 = 1 << 1;

/// Strategy surface the collector reads. Implemented by the IR interpreter.
pub(crate) trait AuditSlots {
    fn slot_index(&self, name: &str) -> Option<usize>;
    fn slot_f64(&self, index: usize) -> f64;
    fn slot_bool(&self, index: usize) -> bool;
}

#[derive(Clone, Copy)]
struct SlotMap {
    priorsessionclose: usize,
    priorsessionunadjustedclose: usize,
    priorsessiontr: usize,
    c3scaledlevel: usize,
    c4cappedrange: usize,
    c4traildistance: usize,
    historyready: usize,
    c1gate: usize,
    entrygate: usize,
    ladderbarselapsed: usize,
    bestclosesincesignal: usize,
    trailstop: usize,
    emergencyexit: usize,
    eodexit: usize,
}

impl SlotMap {
    fn bind<S: AuditSlots>(strategy: &S) -> Result<Self, KernelError> {
        let req = |name: &str| {
            strategy.slot_index(name).ok_or_else(|| {
                KernelError::input(format!("audit-event-v1 missing interpreter slot {name:?}"))
            })
        };
        Ok(Self {
            priorsessionclose: req("priorsessionclose")?,
            priorsessionunadjustedclose: req("priorsessionunadjustedclose")?,
            priorsessiontr: req("priorsessiontr")?,
            c3scaledlevel: req("c3scaledlevel")?,
            c4cappedrange: req("c4cappedrange")?,
            c4traildistance: req("c4traildistance")?,
            historyready: req("historyready")?,
            c1gate: req("c1gate")?,
            entrygate: req("entrygate")?,
            ladderbarselapsed: req("ladderbarselapsed")?,
            bestclosesincesignal: req("bestclosesincesignal")?,
            trailstop: req("trailstop")?,
            emergencyexit: req("emergencyexit")?,
            eodexit: req("eodexit")?,
        })
    }
}

struct EntryContext {
    bar: u32,
    date: i32,
    time: u16,
    fill_price: f64,
    true_range: f64,
    true_close: f64,
    yesterday_close: f64,
}

/// Collects typed transition + completed-trade evidence after `after_bar`.
pub(crate) struct AuditCollector {
    slots: SlotMap,
    tick: f64,
    point_value: f64,
    round_turn_cost: f64,
    previous_contracts: isize,
    previous_average: f64,
    previous_net_profit: f64,
    open_entries: VecDeque<EntryContext>,
    transitions: Vec<[u8; TRANSITION_SIZE]>,
    trades: Vec<[u8; TRADE_SIZE]>,
    campaigns: u32,
    cumulative: f64,
    peak: f64,
}

impl AuditCollector {
    pub(crate) fn bind<S: AuditSlots>(
        strategy: &S,
        tick: f64,
        point_value: f64,
        round_turn_cost: f64,
    ) -> Result<Self, KernelError> {
        if !(tick.is_finite() && tick > 0.0) {
            return Err(KernelError::input(format!(
                "audit-event-v1 tick must be a positive finite value, got {tick}"
            )));
        }
        Ok(Self {
            slots: SlotMap::bind(strategy)?,
            tick,
            point_value,
            round_turn_cost,
            previous_contracts: 0,
            previous_average: 0.0,
            previous_net_profit: 0.0,
            open_entries: VecDeque::new(),
            transitions: Vec::new(),
            trades: Vec::new(),
            campaigns: 0,
            cumulative: 0.0,
            peak: 0.0,
        })
    }

    pub(crate) fn observe<S: AuditSlots>(
        &mut self,
        strategy: &S,
        pos: PositionView,
        bar: Bar,
        index: usize,
    ) -> Result<(), KernelError> {
        let contracts = pos.contracts;
        if contracts < 0 {
            return Err(KernelError::input(format!(
                "audit-event-v1 does not collect short positions (bar {} contracts {contracts})",
                index + 1
            )));
        }
        if contracts == self.previous_contracts {
            return Ok(());
        }

        let bar_index = u32::try_from(index + 1)
            .map_err(|_| KernelError::input(format!("bar index {} does not fit u32", index + 1)))?;
        let average = pos.entry_price;
        let net_profit = pos.net_profit;
        let kind = if contracts > self.previous_contracts {
            KIND_SCALE_IN
        } else {
            KIND_FULL_EXIT
        };

        self.transitions.push(encode_transition(
            strategy,
            &self.slots,
            pos,
            bar_index,
            kind,
        ));

        if contracts > self.previous_contracts {
            let added = contracts - self.previous_contracts;
            let fill_price = snap_tick(
                (average * contracts as f64
                    - self.previous_average * self.previous_contracts as f64)
                    / added as f64,
                self.tick,
            );
            let date = el_date_i32(bar.date)?;
            let time = el_time_u16(bar.time)?;
            for _ in 0..added {
                self.open_entries.push_back(EntryContext {
                    bar: bar_index,
                    date,
                    time,
                    fill_price,
                    true_range: strategy.slot_f64(self.slots.priorsessiontr),
                    true_close: strategy.slot_f64(self.slots.priorsessionunadjustedclose),
                    yesterday_close: strategy.slot_f64(self.slots.priorsessionclose),
                });
            }
        } else {
            let removed = self.previous_contracts - contracts;
            if contracts != 0 || removed != self.open_entries.len() as isize {
                return Err(KernelError::input(format!(
                    "audit-event-v1 unsupported transition {} -> {} at bar {} (open {})",
                    self.previous_contracts,
                    contracts,
                    bar_index,
                    self.open_entries.len()
                )));
            }
            let exit_price = snap_tick(
                self.previous_average
                    + (net_profit - self.previous_net_profit)
                        / (self.point_value * self.previous_contracts as f64),
                self.tick,
            );
            let exit_date = el_date_i32(bar.date)?;
            let exit_time = el_time_u16(bar.time)?;
            while let Some(entry) = self.open_entries.pop_front() {
                let gross = (exit_price - entry.fill_price) * self.point_value;
                let net = gross - self.round_turn_cost;
                self.cumulative += net;
                self.peak = self.peak.max(self.cumulative);
                let drawdown = self.cumulative - self.peak;
                self.trades.push(encode_trade(
                    entry.bar,
                    bar_index,
                    entry.date,
                    exit_date,
                    entry.time,
                    exit_time,
                    entry.fill_price,
                    exit_price,
                    net,
                    entry.true_range,
                    entry.true_close,
                    entry.yesterday_close,
                    self.cumulative,
                    drawdown,
                ));
            }
            self.campaigns += 1;
        }

        self.previous_contracts = contracts;
        self.previous_average = average;
        self.previous_net_profit = net_profit;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn finish(
        self,
        outcome: &KernelOutcome,
        tape: &BarTape,
        program_ir: &str,
        config: &Config,
        fill_digest: [u8; 32],
        hash_tapes: bool,
        source_bar_count: usize,
    ) -> Vec<u8> {
        let remaining_open = u32::try_from(self.open_entries.len()).unwrap_or(u32::MAX);
        let mut flags = 0u32;
        let hash_data1 = if hash_tapes {
            flags |= FLAG_HASH_DATA1;
            hash_tape(tape)
        } else {
            [0u8; 32]
        };
        let hash_data2 = [0u8; 32];
        let hash_ir = sha256_bytes(program_ir.as_bytes());
        let hash_config = hash_config(config);

        let mut records = Vec::with_capacity(
            self.transitions.len() * TRANSITION_SIZE + self.trades.len() * TRADE_SIZE,
        );
        for row in &self.transitions {
            records.extend_from_slice(row);
        }
        for row in &self.trades {
            records.extend_from_slice(row);
        }
        let hash_records = sha256_bytes(&records);

        let fill_count = u32::try_from(outcome.fills.len()).unwrap_or(u32::MAX);
        let round_turns = exit_record_count(&outcome.fills);
        let ending = i32::try_from(outcome.position.contracts).unwrap_or(i32::MAX);
        let eval_from = u32::try_from(outcome.skipped_bars).unwrap_or(u32::MAX);

        let mut header = [0u8; HEADER_SIZE];
        header[0..8].copy_from_slice(MAGIC);
        header[8..10].copy_from_slice(&VERSION.to_le_bytes());
        header[10..12].copy_from_slice(&(HEADER_SIZE as u16).to_le_bytes());
        header[12..16].copy_from_slice(&flags.to_le_bytes());
        header[16..20].copy_from_slice(&(source_bar_count as u32).to_le_bytes());
        header[20..24].copy_from_slice(&(self.transitions.len() as u32).to_le_bytes());
        header[24..28].copy_from_slice(&(self.trades.len() as u32).to_le_bytes());
        header[28..30].copy_from_slice(&(TRANSITION_SIZE as u16).to_le_bytes());
        header[30..32].copy_from_slice(&(TRADE_SIZE as u16).to_le_bytes());
        header[32..36].copy_from_slice(&ending.to_le_bytes());
        header[36..40].copy_from_slice(&self.campaigns.to_le_bytes());
        header[40..48].copy_from_slice(&outcome.position.net_profit.to_le_bytes());
        header[48..52].copy_from_slice(&fill_count.to_le_bytes());
        header[52..56].copy_from_slice(&round_turns.to_le_bytes());
        header[56..60].copy_from_slice(&eval_from.to_le_bytes());
        header[60..64].copy_from_slice(&remaining_open.to_le_bytes());
        header[64..96].copy_from_slice(&hash_data1);
        header[96..128].copy_from_slice(&hash_data2);
        header[128..160].copy_from_slice(&hash_ir);
        header[160..192].copy_from_slice(&hash_config);
        header[192..224].copy_from_slice(&fill_digest);
        header[224..256].copy_from_slice(&hash_records);

        let mut out = Vec::with_capacity(HEADER_SIZE + records.len());
        out.extend_from_slice(&header);
        out.extend_from_slice(&records);
        let _ = FLAG_HASH_DATA2; // reserved: Data2 hashing is opt-in later
        out
    }
}

fn encode_transition<S: AuditSlots>(
    strategy: &S,
    slots: &SlotMap,
    pos: PositionView,
    bar_index: u32,
    kind: u8,
) -> [u8; TRANSITION_SIZE] {
    let mut mask = 0u8;
    if strategy.slot_bool(slots.historyready) {
        mask |= 1 << 0;
    }
    if strategy.slot_bool(slots.c1gate) {
        mask |= 1 << 1;
    }
    if strategy.slot_bool(slots.entrygate) {
        mask |= 1 << 2;
    }
    if strategy.slot_bool(slots.emergencyexit) {
        mask |= 1 << 3;
    }
    if strategy.slot_bool(slots.eodexit) {
        mask |= 1 << 4;
    }
    let ladder = strategy.slot_f64(slots.ladderbarselapsed) as i32;
    let mut row = [0u8; TRANSITION_SIZE];
    row[0..4].copy_from_slice(&bar_index.to_le_bytes());
    row[4] = kind;
    row[5] = mask;
    row[6] = pos.market_position as u8;
    row[7] = 0;
    row[8..12].copy_from_slice(&(pos.contracts as i32).to_le_bytes());
    row[12..16].copy_from_slice(&(pos.total_trades as u32).to_le_bytes());
    row[16..20].copy_from_slice(&ladder.to_le_bytes());
    // 20..24 pad
    write_f64(&mut row, 24, pos.entry_price);
    write_f64(&mut row, 32, pos.net_profit);
    write_f64(&mut row, 40, strategy.slot_f64(slots.priorsessionclose));
    write_f64(
        &mut row,
        48,
        strategy.slot_f64(slots.priorsessionunadjustedclose),
    );
    write_f64(&mut row, 56, strategy.slot_f64(slots.priorsessiontr));
    write_f64(&mut row, 64, strategy.slot_f64(slots.c3scaledlevel));
    write_f64(&mut row, 72, strategy.slot_f64(slots.c4cappedrange));
    write_f64(&mut row, 80, strategy.slot_f64(slots.c4traildistance));
    write_f64(&mut row, 88, strategy.slot_f64(slots.bestclosesincesignal));
    write_f64(&mut row, 96, strategy.slot_f64(slots.trailstop));
    row
}

#[allow(clippy::too_many_arguments)]
fn encode_trade(
    entry_bar: u32,
    exit_bar: u32,
    entry_date: i32,
    exit_date: i32,
    entry_time: u16,
    exit_time: u16,
    entry_price: f64,
    exit_price: f64,
    net_after_cost: f64,
    true_range: f64,
    true_close: f64,
    yesterday_close: f64,
    cumulative: f64,
    drawdown: f64,
) -> [u8; TRADE_SIZE] {
    let mut row = [0u8; TRADE_SIZE];
    row[0..4].copy_from_slice(&entry_bar.to_le_bytes());
    row[4..8].copy_from_slice(&exit_bar.to_le_bytes());
    row[8..12].copy_from_slice(&entry_date.to_le_bytes());
    row[12..16].copy_from_slice(&exit_date.to_le_bytes());
    row[16..18].copy_from_slice(&entry_time.to_le_bytes());
    row[18..20].copy_from_slice(&exit_time.to_le_bytes());
    row[20] = DIR_LONG;
    row[21] = 0;
    row[22..24].copy_from_slice(&1u16.to_le_bytes());
    write_f64(&mut row, 24, entry_price);
    write_f64(&mut row, 32, exit_price);
    write_f64(&mut row, 40, net_after_cost);
    write_f64(&mut row, 48, true_range);
    write_f64(&mut row, 56, true_close);
    write_f64(&mut row, 64, yesterday_close);
    write_f64(&mut row, 72, cumulative);
    write_f64(&mut row, 80, drawdown);
    row
}

fn write_f64(row: &mut [u8], offset: usize, value: f64) {
    row[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn el_date_i32(date: i64) -> Result<i32, KernelError> {
    i32::try_from(date).map_err(|_| KernelError::input(format!("EL date {date} does not fit i32")))
}

fn el_time_u16(time: i64) -> Result<u16, KernelError> {
    u16::try_from(time).map_err(|_| KernelError::input(format!("EL time {time} does not fit u16")))
}

/// Python 3 `round(value / tick) * tick` (ties to even).
pub(crate) fn snap_tick(value: f64, tick: f64) -> f64 {
    python_round_half_even(value / tick) * tick
}

/// Python 3 `round(x)` for a finite float: nearest integer, ties toward even.
pub(crate) fn python_round_half_even(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let fl = x.floor();
    let frac = x - fl;
    if frac < 0.5 {
        fl
    } else if frac > 0.5 {
        fl + 1.0
    } else {
        let n = fl as i64;
        if n.rem_euclid(2) == 0 {
            fl
        } else {
            fl + 1.0
        }
    }
}

pub(crate) fn sha256_bytes(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

pub(crate) fn fill_digest_from_lines<I, S>(lines: I) -> [u8; 32]
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut hasher = Sha256::new();
    // pl_oracle digest v2: the hashed text starts with a version line.
    hasher.update(b"digest_version: 2\n");
    for line in lines {
        hasher.update(line.as_ref().as_bytes());
        hasher.update(b"\n");
    }
    hasher.finalize().into()
}

/// Round turns as pl_oracle v2 counts them: one per exit record, i.e. per
/// fill that moves the signed position toward flat.
fn exit_record_count(fills: &[Fill]) -> u32 {
    let mut pos: i64 = 0;
    let mut exits = 0u32;
    for fill in fills {
        let step = if fill.fill_type == "EntryLong" { 1 } else { -1 };
        if pos != 0 && pos.signum() != step {
            exits = exits.saturating_add(1);
        }
        pos += step;
    }
    exits
}

fn hash_config(config: &Config) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(config.name.as_bytes());
    hasher.update(b"\n");
    hasher.update(config.bigpointvalue.to_le_bytes());
    hasher.update(config.pointvalue.to_le_bytes());
    hasher.update(config.minmove.to_le_bytes());
    hasher.update(config.pricescale.to_le_bytes());
    hasher.update(config.tick.to_le_bytes());
    hasher.update(config.barinterval.to_le_bytes());
    hasher.update(config.session.start.to_le_bytes());
    hasher.update(config.session.end.to_le_bytes());
    hasher.update(config.commission.to_le_bytes());
    hasher.update(config.slippage.to_le_bytes());
    hasher.update((config.max_bars_back as u64).to_le_bytes());
    hasher.update((config.warmup_seed as u64).to_le_bytes());
    hasher.update(config.symccy.to_le_bytes());
    hasher.update(config.symnm.as_bytes());
    hasher.update(b"\n");
    hasher.update(config.symalt.as_bytes());
    hasher.update(b"\n");
    hasher.update(config.descstr.as_bytes());
    hasher.update(b"\n");
    hasher.update(config.expdate.to_le_bytes());
    hasher.finalize().into()
}

fn hash_tape(tape: &BarTape) -> [u8; 32] {
    let mut hasher = Sha256::new();
    for i in 0..tape.len() {
        let bar = tape.bar(i);
        hasher.update(bar.open.to_le_bytes());
        hasher.update(bar.high.to_le_bytes());
        hasher.update(bar.low.to_le_bytes());
        hasher.update(bar.close.to_le_bytes());
        hasher.update(bar.volume.to_le_bytes());
        hasher.update(bar.date.to_le_bytes());
        hasher.update(bar.time.to_le_bytes());
    }
    hasher.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn side(buy: bool) -> Fill {
        Fill {
            fill_type: crate::types::fill_side_type(buy),
            signal: String::new(),
            date: 0,
            time: 0,
            price: 0.0,
            contracts: 1,
        }
    }

    struct ZeroSlots;

    impl AuditSlots for ZeroSlots {
        fn slot_index(&self, name: &str) -> Option<usize> {
            NQ_AUDIT_SLOT_NAMES.iter().position(|n| *n == name)
        }
        fn slot_f64(&self, _index: usize) -> f64 {
            0.0
        }
        fn slot_bool(&self, _index: usize) -> bool {
            false
        }
    }

    fn view(contracts: isize, entry_price: f64, net_profit: f64) -> PositionView {
        PositionView {
            market_position: contracts.signum() as i8,
            contracts,
            entry_price,
            avg_entry_price: entry_price,
            bars_since_entry: 0,
            exit_price: 0.0,
            bars_since_exit: 0,
            open_position_profit: 0.0,
            position_profit: 0.0,
            net_profit,
            gross_profit: 0.0,
            gross_loss: 0.0,
            total_trades: 0,
            max_contracts_held: contracts,
            max_id_drawdown: 0.0,
        }
    }

    fn bar(date: i64, time: i64) -> Bar {
        Bar {
            open: 0.0,
            high: 0.0,
            low: 0.0,
            close: 0.0,
            volume: 0.0,
            date,
            time,
        }
    }

    /// One long round turn, 100.00 -> 101.00 at $20/pt: gross $20.
    fn net_after(cost: f64) -> f64 {
        let mut c = AuditCollector::bind(&ZeroSlots, 0.25, 20.0, cost).unwrap();
        c.observe(&ZeroSlots, view(1, 100.0, 0.0), bar(1180102, 930), 0)
            .unwrap();
        c.observe(&ZeroSlots, view(0, 0.0, 20.0), bar(1180102, 931), 1)
            .unwrap();
        assert_eq!(c.trades.len(), 1);
        f64::from_le_bytes(c.trades[0][40..48].try_into().unwrap())
    }

    #[test]
    fn trade_net_is_gross_minus_configured_round_turn_cost() {
        assert_eq!(net_after(0.0), 20.0);
        assert_eq!(net_after(25.0), -5.0);
        assert_eq!(net_after(4.5), 15.5);
    }

    #[test]
    fn round_turns_count_exit_records() {
        // Buy, reversal (exit + entry), cover: two exits.
        let rev = [side(true), side(false), side(false), side(true)];
        assert_eq!(exit_record_count(&rev), 2);
        // Long 2 -> short 1: two exits, the last sell opens the short.
        let l2s1 = [
            side(true),
            side(true),
            side(false),
            side(false),
            side(false),
        ];
        assert_eq!(exit_record_count(&l2s1), 2);
        assert_eq!(exit_record_count(&[]), 0);
    }

    #[test]
    fn fill_digest_starts_with_the_version_line() {
        let want: [u8; 32] = Sha256::digest(b"digest_version: 2\nx\n").into();
        assert_eq!(fill_digest_from_lines(["x"]), want);
    }

    #[test]
    fn header_and_record_sizes_match_the_spike_schema() {
        assert_eq!(HEADER_SIZE, 256);
        assert_eq!(TRANSITION_SIZE, 104);
        assert_eq!(TRADE_SIZE, 88);
        assert_eq!(MAGIC, b"AUEVT1\0\0");
        assert_eq!(NQ_AUDIT_SLOT_NAMES.len(), 14);
        assert!(!NQ_AUDIT_SLOT_NAMES.contains(&"priorsessionweightedmean"));
        assert!(!NQ_AUDIT_SLOT_NAMES.contains(&"c3entrylevel"));
    }

    #[test]
    fn python_round_half_even_matches_cpython3() {
        assert_eq!(python_round_half_even(0.5), 0.0);
        assert_eq!(python_round_half_even(1.5), 2.0);
        assert_eq!(python_round_half_even(2.5), 2.0);
        assert_eq!(python_round_half_even(3.5), 4.0);
        assert_eq!(python_round_half_even(-0.5), 0.0);
        assert_eq!(python_round_half_even(-1.5), -2.0);
        assert_eq!(python_round_half_even(2.0), 2.0);
        assert_eq!(python_round_half_even(2.6), 3.0);
    }

    #[test]
    fn snap_tick_nq_quarter() {
        assert_eq!(snap_tick(10131.0, 0.25), 10131.0);
        assert_eq!(snap_tick(10131.12, 0.25), 10131.0);
        assert_eq!(snap_tick(10131.13, 0.25), 10131.25);
        // 10131.125 / 0.25 = 40524.5 → even 40524 → 10131.0
        assert_eq!(snap_tick(10131.125, 0.25), 10131.0);
        // 10131.375 / 0.25 = 40525.5 → even 40526 → 10131.5
        assert_eq!(snap_tick(10131.375, 0.25), 10131.5);
    }
}
