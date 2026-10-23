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

//! Drawing objects recorded by the native kernel.
//!
//! The values are the MultiCharts measurements cited by the golden corpus
//! (g-tl-001-new, g-arw-001-new, g-text-001-new, g-colour-001-hours). An
//! unmeasured colour call is an error. An anchor need not be a bar on the
//! tape: it keeps its raw (date, time) and has no `bn`, as on the Python side. `TL_GetValue` interpolation is not implemented here: the
//! compiler still refuses that call.

use serde_json::{json, Value as Json};

use crate::types::KernelError;

// g-tl-001-new
const TL_COLOR: i64 = 36095;
const TL_STYLE: i64 = 1;
const TL_SIZE: i64 = 0;
// g-arw-001-new
const ARW_COLOR_UP: i64 = 16_711_680;
const ARW_COLOR_DOWN: i64 = 255;
const ARW_STYLE: i64 = 10;
const ARW_SIZE: i64 = 1;
// g-text-001-new. The font name is recorded as "?" because the readback does not report it.
const TEXT_COLOR: i64 = 7_058_764;
const TEXT_HSTYLE: i64 = 2;
const TEXT_VSTYLE: i64 = 2;
const TEXT_FONT_SIZE: i64 = 9;
const TEXT_BGCOLOR: i64 = 0;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Tl,
    Arw,
    Text,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Self::Tl => "tl",
            Self::Arw => "arw",
            Self::Text => "text",
        }
    }

    fn index(self) -> usize {
        match self {
            Self::Tl => 0,
            Self::Arw => 1,
            Self::Text => 2,
        }
    }
}

pub(crate) struct Anchor {
    bn: Option<i64>,
    date: i64,
    time: i64,
    price: f64,
}

struct DrawObj {
    kind: Kind,
    id: i64,
    created_bn: i64,
    created_date: i64,
    created_time: i64,
    anchors: Vec<Anchor>,
    color: i64,
    style: Option<i64>,
    size: Option<i64>,
    ext_left: Option<bool>,
    ext_right: Option<bool>,
    text: Option<String>,
    hstyle: Option<i64>,
    vstyle: Option<i64>,
    font: Option<String>,
    font_size: Option<i64>,
    bgcolor: Option<i64>,
    border: Option<bool>,
    direction: Option<&'static str>,
}

pub(crate) struct DrawingStore {
    tick: f64,
    ids: [i64; 3],
    objects: Vec<DrawObj>,
}

impl DrawingStore {
    pub(crate) fn new() -> Self {
        Self {
            tick: 0.25,
            ids: [0; 3],
            objects: Vec::new(),
        }
    }

    pub(crate) fn set_tick(&mut self, tick: f64) {
        self.tick = tick;
    }

    pub(crate) fn to_json(&self) -> Json {
        Json::Array(
            self.objects
                .iter()
                .map(|obj| obj.to_json(self.tick))
                .collect(),
        )
    }

    fn live_mut(&mut self, kind: Kind, id: f64) -> Result<&mut DrawObj, KernelError> {
        let id = whole("drawing id", id)?;
        let pos = self
            .objects
            .iter()
            .position(|obj| obj.kind == kind && obj.id == id)
            .ok_or_else(|| {
                KernelError::strategy(format!("no live {} with id {id}", kind.name()))
            })?;
        Ok(&mut self.objects[pos])
    }

    pub(crate) fn tl_new(&mut self, created: (i64, i64, i64), a1: Anchor, a2: Anchor) -> f64 {
        self.push(
            Kind::Tl,
            created,
            vec![a1, a2],
            TL_COLOR,
            Some(TL_STYLE),
            Some(TL_SIZE),
            Some(false),
            Some(false),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    }

    pub(crate) fn tl_set_color(&mut self, id: f64, color: f64) -> Result<f64, KernelError> {
        self.live_mut(Kind::Tl, id)?.color = whole("color", color)?;
        Ok(0.0)
    }

    /// `pl_text_setcolor`: `_live("text", id)["color"] = int(color)`.
    pub(crate) fn text_set_color(&mut self, id: f64, color: f64) -> Result<f64, KernelError> {
        self.live_mut(Kind::Text, id)?.color = whole("color", color)?;
        Ok(0.0)
    }

    pub(crate) fn tl_set_style(&mut self, id: f64, style: f64) -> Result<f64, KernelError> {
        self.live_mut(Kind::Tl, id)?.style = Some(whole("style", style)?);
        Ok(0.0)
    }

    pub(crate) fn arw_new(&mut self, created: (i64, i64, i64), anchor: Anchor, down: bool) -> f64 {
        self.push(
            Kind::Arw,
            created,
            vec![anchor],
            if down { ARW_COLOR_DOWN } else { ARW_COLOR_UP },
            Some(ARW_STYLE),
            Some(ARW_SIZE),
            None,
            None,
            Some(String::new()),
            None,
            None,
            None,
            None,
            None,
            Some(if down { "down" } else { "up" }),
        )
    }

    pub(crate) fn text_new(
        &mut self,
        created: (i64, i64, i64),
        anchor: Anchor,
        text: String,
    ) -> f64 {
        self.push(
            Kind::Text,
            created,
            vec![anchor],
            TEXT_COLOR,
            None,
            None,
            None,
            None,
            Some(text),
            Some(TEXT_HSTYLE),
            Some(TEXT_VSTYLE),
            Some("?".to_string()),
            Some(TEXT_FONT_SIZE),
            Some(TEXT_BGCOLOR),
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        kind: Kind,
        created: (i64, i64, i64),
        anchors: Vec<Anchor>,
        color: i64,
        style: Option<i64>,
        size: Option<i64>,
        ext_left: Option<bool>,
        ext_right: Option<bool>,
        text: Option<String>,
        hstyle: Option<i64>,
        vstyle: Option<i64>,
        font: Option<String>,
        font_size: Option<i64>,
        bgcolor: Option<i64>,
        direction: Option<&'static str>,
    ) -> f64 {
        let slot = kind.index();
        self.ids[slot] += 1;
        let id = self.ids[slot];
        let border = if kind == Kind::Text {
            Some(false)
        } else {
            None
        };
        self.objects.push(DrawObj {
            kind,
            id,
            created_bn: created.0,
            created_date: created.1,
            created_time: created.2,
            anchors,
            color,
            style,
            size,
            ext_left,
            ext_right,
            text,
            hstyle,
            vstyle,
            font,
            font_size,
            bgcolor,
            border,
            direction,
        });
        id as f64
    }
}

impl DrawObj {
    fn to_json(&self, tick: f64) -> Json {
        json!({
            "type": self.kind.name(),
            "seq": 0,
            "id": self.id,
            "created": bar_key(Some(self.created_bn), self.created_date, self.created_time, None, tick),
            "anchors": self.anchors.iter().map(|a| bar_key(a.bn, a.date, a.time, Some(a.price), tick)).collect::<Vec<_>>(),
            "color": self.color,
            "argb": Json::Null,
            "style": opt_i64(self.style),
            "size": opt_i64(self.size),
            "ext_left": opt_bool(self.ext_left),
            "ext_right": opt_bool(self.ext_right),
            "text": self.text.clone().map(Json::String).unwrap_or(Json::Null),
            "hstyle": opt_i64(self.hstyle),
            "vstyle": opt_i64(self.vstyle),
            "font": self.font.clone().map(Json::String).unwrap_or(Json::Null),
            "font_size": opt_i64(self.font_size),
            "bgcolor": opt_i64(self.bgcolor),
            "border": opt_bool(self.border),
            "direction": self.direction.map(|s| Json::String(s.to_string())).unwrap_or(Json::Null),
            "deleted": Json::Null,
        })
    }
}

fn bar_key(bn: Option<i64>, date: i64, time: i64, price: Option<f64>, tick: f64) -> Json {
    let mut key = json!({
        "bn": bn,
        "date": date,
        "time": time,
        "time_s": time * 100,
    });
    if let Some(price) = price {
        // Float32, as on the Python side; ticks round half to even like
        // Python's round().
        let price = price as f32 as f64;
        let ticks = if tick > 0.0 {
            (price / tick).round_ties_even() as i64
        } else {
            0
        };
        key["price"] = json!(price);
        key["price_ticks"] = json!(ticks);
    }
    key
}

fn opt_i64(value: Option<i64>) -> Json {
    value.map(|n| json!(n)).unwrap_or(Json::Null)
}

fn opt_bool(value: Option<bool>) -> Json {
    value.map(Json::Bool).unwrap_or(Json::Null)
}

pub(crate) fn find_anchor(
    dates: &[i64],
    times: &[i64],
    date: f64,
    time: f64,
    price: f64,
) -> Result<Anchor, KernelError> {
    let date = whole("anchor date", date)?;
    let time = whole("anchor time", time)?;
    // `None` when the anchor is not a tape bar, e.g. (0, 0) from an
    // out-of-history lookback or a session-end time.
    let mut found = None;
    for index in 1..dates.len() {
        if dates[index] == date && times[index] == time {
            if found.is_some() {
                return Err(KernelError::strategy(format!(
                    "duplicate tape bar date={date} time={time}"
                )));
            }
            found = Some(index as i64);
        }
    }
    Ok(Anchor {
        bn: found,
        date,
        time,
        price,
    })
}

fn whole(what: &str, value: f64) -> Result<i64, KernelError> {
    if !value.is_finite() {
        return Err(KernelError::strategy(format!(
            "{what} {value} is not finite"
        )));
    }
    let truncated = value.trunc();
    if (value - truncated).abs() > 1e-6 {
        return Err(KernelError::strategy(format!(
            "{what} {value} is not an integer"
        )));
    }
    Ok(truncated as i64)
}

pub(crate) fn rgb(red: f64, green: f64, blue: f64) -> Result<f64, KernelError> {
    let r = channel("RGB", red)?;
    let g = channel("RGB", green)?;
    let b = channel("RGB", blue)?;
    Ok((r + 256 * g + 65536 * b) as f64)
}

pub(crate) fn legacy_color_to_rgb(index: f64) -> Result<f64, KernelError> {
    // The only measured point is LegacyColorToRGB(14) = 32896 (g-colour-001-hours).
    let key = whole("LegacyColorToRGB", index)?;
    if key == 14 {
        Ok(32896.0)
    } else {
        Err(KernelError::strategy(format!(
            "LegacyColorToRGB({key}) is not measured; refusing to guess a palette"
        )))
    }
}

pub(crate) fn gradient_color(
    value: f64,
    min_value: f64,
    max_value: f64,
    start: f64,
    end: f64,
) -> Result<f64, KernelError> {
    let span = max_value - min_value;
    if span == 0.0 || !span.is_finite() {
        return Err(KernelError::strategy(
            "GradientColor with Min = Max is not measured; refusing to guess",
        ));
    }
    let frac = (value - min_value) / span;
    let (sr, sg, sb) = split_color(start)?;
    let (er, eg, eb) = split_color(end)?;
    let chan = |start_c: i64, end_c: i64| -> i64 {
        start_c + ((end_c - start_c) as f64 * frac).trunc() as i64
    };
    rgb(
        chan(sr, er) as f64,
        chan(sg, eg) as f64,
        chan(sb, eb) as f64,
    )
}

fn channel(where_: &str, value: f64) -> Result<i64, KernelError> {
    let channel = whole(where_, value)?;
    if !(0..=255).contains(&channel) {
        return Err(KernelError::strategy(format!(
            "{where_} channel {channel} is outside 0..255; MultiCharts behaviour there is not measured"
        )));
    }
    Ok(channel)
}

fn split_color(color: f64) -> Result<(i64, i64, i64), KernelError> {
    let packed = whole("colour", color)?;
    if !(0..=0xFFFFFF).contains(&packed) {
        return Err(KernelError::strategy(format!(
            "colour {packed} is outside the measured 24-bit range"
        )));
    }
    Ok((packed & 0xFF, (packed >> 8) & 0xFF, (packed >> 16) & 0xFF))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gradient_midpoint_matches_the_corpus() {
        // GradientColor(5, 0, 10, Red, Blue) = 8323200 (g-colour-001-hours).
        let value = gradient_color(5.0, 0.0, 10.0, 255.0, 16_711_680.0).unwrap();
        assert_eq!(value, 8_323_200.0);
        assert_eq!(rgb(10.0, 20.0, 30.0).unwrap(), 1_971_210.0);
        assert_eq!(legacy_color_to_rgb(14.0).unwrap(), 32_896.0);
        assert!(legacy_color_to_rgb(1.0).is_err());
        assert!(gradient_color(1.0, 1.0, 1.0, 255.0, 0.0).is_err());
    }

    #[test]
    fn trendline_anchor_uses_the_tape_index() {
        let mut store = DrawingStore::new();
        let dates = vec![0, 1260102, 1260102];
        let times = vec![0, 1000, 1001];
        let a1 = find_anchor(&dates, &times, 1260102.0, 1000.0, 10.0).unwrap();
        let a2 = find_anchor(&dates, &times, 1260102.0, 1001.0, 14.0).unwrap();
        let id = store.tl_new((2, 1260102, 1001), a1, a2);
        assert_eq!(id, 1.0);
        let dumped = store.to_json();
        let obj = &dumped[0];
        assert_eq!(obj["color"], 36095);
        assert_eq!(obj["style"], 1);
        assert_eq!(obj["anchors"][0]["bn"], 1);
        assert_eq!(obj["anchors"][0]["price_ticks"], 40);
        assert_eq!(obj["anchors"][1]["bn"], 2);
        assert_eq!(obj["created"]["bn"], 2);
    }

    #[test]
    fn anchor_price_is_float32_and_ticks_round_half_to_even() {
        let key = bar_key(Some(1), 1260102, 1000, Some(14957.45), 0.25);
        assert_eq!(key["price"], json!(14957.45_f32 as f64));
        assert_eq!(key["price_ticks"], 59830);
        // 10.125 / 0.25 = 40.5, which Python's round() takes to 40.
        assert_eq!(
            bar_key(Some(1), 1, 1, Some(10.125), 0.25)["price_ticks"],
            40
        );
    }

    #[test]
    fn anchor_off_the_tape_keeps_its_date_and_time_with_null_bn() {
        let mut store = DrawingStore::new();
        let dates = vec![0, 1260102, 1260102];
        let times = vec![0, 1000, 1001];
        // Date[1], Time[1] out of history on bar 1, and a session-end time.
        let a1 = find_anchor(&dates, &times, 0.0, 0.0, 10.0).unwrap();
        let a2 = find_anchor(&dates, &times, 1260102.0, 1615.0, 14.0).unwrap();
        store.tl_new((1, 1260102, 1000), a1, a2);
        let dumped = store.to_json();
        let anchors = &dumped[0]["anchors"];
        assert_eq!(anchors[0]["bn"], Json::Null);
        assert_eq!(anchors[0]["date"], 0);
        assert_eq!(anchors[0]["time_s"], 0);
        assert_eq!(anchors[1]["bn"], Json::Null);
        assert_eq!(anchors[1]["time"], 1615);
        assert_eq!(anchors[1]["time_s"], 161500);
        assert_eq!(anchors[1]["price_ticks"], 56);
        // Between bars and in the future.
        let between = find_anchor(&dates, &times, 1260102.0, 1030.0, 1.0).unwrap();
        assert_eq!(between.bn, None);
        let future = find_anchor(&dates, &times, 1301231.0, 930.0, 1.0).unwrap();
        assert_eq!(future.bn, None);
    }
}
