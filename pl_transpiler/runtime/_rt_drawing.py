# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PowerLanguage runtime: trendline, text and arrow drawing objects.

Re-exported by pl_runtime. Without an active :class:`DrawingRecorder` the
creators still return ids (a process-wide counter) and the setters are
no-ops, so a run that does not ask for a drawing dump keeps its previous
trade output. With a recorder (``run_el`` / ``run_oracle`` ``drawings=True``)
each creator records an object keyed to the tape, and ids restart at 1 per
object type per run.

Creation colours and arrow/text styles are the MultiCharts defaults measured
on the golden corpus (g-tl-001-new, g-arw-001-new, g-text-001-new).
"""
import contextvars

from ._rt_core import UnimplementedBuiltinError
from .drawing_dump import barkey, blank_object


class DrawingGetterError(UnimplementedBuiltinError):
    """A drawing getter cannot answer. The run stops, including in lenient mode."""

    def __init__(self, detail):
        self.builtin = "drawing"
        NotImplementedError.__init__(self, detail)

_RECORDER = contextvars.ContextVar("pb_drawing_recorder", default=None)

# Process-wide fallback used only when no recorder is active. A recorder
# resets per run and does not touch this counter.
_drawing_id_counter = 0

# g-tl-001-new: default TL colour 36095, style 1 (solid), size 0, no extensions.
_TL_COLOR = 36095
_TL_STYLE = 1
_TL_SIZE = 0
# g-arw-001-new: up 16711680, down 255, style 10, size 1, text "".
_ARW_COLOR_UP = 16711680
_ARW_COLOR_DOWN = 255
_ARW_STYLE = 10
_ARW_SIZE = 1
# g-text-001-new. font "?" because the readback does not report the face.
_TEXT_COLOR = 7058764
_TEXT_HSTYLE = 2
_TEXT_VSTYLE = 2
_TEXT_FONT_SIZE = 9
_TEXT_BGCOLOR = 0
_TEXT_BORDER = False
_TEXT_FONT = "?"


class DrawingError(RuntimeError):
    """A drawing call could not be recorded faithfully."""


class DrawingRecorder:
    """Per-run drawing objects. Use as a context manager around the bar loop.

    ``dates`` and ``times`` are the tape the run iterates, in order. ``bn``
    is the 1-based index into that tape. The bar loop calls :meth:`set_bar`
    with the 0-based index before each strategy call.
    """

    def __init__(self, dates, times, tick=0.25):
        self.tick = float(tick)
        self._dates = dates
        self._times = times
        self._bn_of = {}
        for index, (date, time) in enumerate(zip(dates, times)):
            key = (int(date), int(time))
            if key in self._bn_of:
                raise DrawingError(
                    f"duplicate tape bar date={key[0]} time={key[1]}")
            self._bn_of[key] = index + 1
        self.bar_index = 0
        self.objects = []
        self._by_id = {}
        self._ids = {"tl": 0, "arw": 0, "text": 0}
        self._token = None

    def set_bar(self, index):
        """0-based tape index of the bar about to be evaluated."""
        self.bar_index = int(index)

    def __enter__(self):
        self._token = _RECORDER.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        _RECORDER.reset(self._token)
        self._token = None
        return False

    def _created(self):
        index = self.bar_index
        return barkey(
            index + 1, int(self._dates[index]), int(self._times[index]))

    def _anchor(self, date, time, price):
        key = (int(date), int(time))
        bn = self._bn_of.get(key)
        if bn is None:
            raise DrawingError(f"anchor ({key[0]}, {key[1]}) is not a tape bar")
        return barkey(bn, key[0], key[1], float(price), self.tick)

    def _new(self, kind, anchors, **props):
        self._ids[kind] += 1
        oid = self._ids[kind]
        obj = blank_object(kind, 0)
        obj.update(
            id=oid, created=self._created(), anchors=anchors, **props)
        self.objects.append(obj)
        self._by_id[(kind, oid)] = obj
        return oid

    def _live(self, kind, oid):
        try:
            oid = int(oid)
        except (TypeError, ValueError):
            raise DrawingError(f"{kind} id {oid!r} is not an integer") from None
        obj = self._by_id.get((kind, oid))
        if obj is None or obj["deleted"] is not None:
            raise DrawingError(f"no live {kind} with id {oid}")
        return obj

    def tl_new(self, d1, t1, p1, d2, t2, p2):
        return self._new(
            "tl",
            [self._anchor(d1, t1, p1), self._anchor(d2, t2, p2)],
            color=_TL_COLOR, style=_TL_STYLE, size=_TL_SIZE,
            ext_left=False, ext_right=False,
        )

    def tl_set(self, oid, field, value):
        self._live("tl", oid)[field] = value
        return 0

    def tl_delete(self, oid):
        obj = self._live("tl", oid)
        obj["deleted"] = self._created()
        return 0

    def arw_new(self, date, time, price, down):
        return self._new(
            "arw",
            [self._anchor(date, time, price)],
            color=_ARW_COLOR_DOWN if down else _ARW_COLOR_UP,
            style=_ARW_STYLE, size=_ARW_SIZE,
            direction="down" if down else "up", text="",
        )

    def text_new(self, date, time, price, text):
        return self._new(
            "text",
            [self._anchor(date, time, price)],
            color=_TEXT_COLOR, text=str(text),
            hstyle=_TEXT_HSTYLE, vstyle=_TEXT_VSTYLE,
            font_size=_TEXT_FONT_SIZE, bgcolor=_TEXT_BGCOLOR,
            border=_TEXT_BORDER, font=_TEXT_FONT,
        )


def _recorder():
    return _RECORDER.get()


def _legacy_id():
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def _as_bool(value):
    """EL true/false, including a numeric 0/1 that survived as a float."""
    if isinstance(value, str):
        raise DrawingError(f"expected a boolean, got {value!r}")
    return bool(value)


def pl_tl_new(d1, t1, p1, d2, t2, p2):
    """Create a trendline and return its id."""
    rec = _recorder()
    if rec is None:
        return _legacy_id()
    return rec.tl_new(d1, t1, p1, d2, t2, p2)


def pl_tl_setcolor(tl_id, color):
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_set(tl_id, "color", int(color))


def pl_tl_setstyle(tl_id, style):
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_set(tl_id, "style", int(style))


def pl_tl_setwidth(tl_id, width):
    """TL_SetWidth writes the dump's ``size`` field (same slot as TL_SetSize)."""
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_set(tl_id, "size", int(width))


def pl_tl_setsize(tl_id, size):
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_set(tl_id, "size", int(size))


def pl_tl_delete(tl_id):
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_delete(tl_id)


def pl_text_new(d, t, p, s):
    """Create a text object and return its id."""
    rec = _recorder()
    if rec is None:
        return _legacy_id()
    return rec.text_new(d, t, p, s)


def pl_text_setstring(text_id, s):
    rec = _recorder()
    if rec is None:
        return 0
    rec._live("text", text_id)["text"] = str(s)
    return 0


def pl_text_setcolor(text_id, color):
    rec = _recorder()
    if rec is None:
        return 0
    rec._live("text", text_id)["color"] = int(color)
    return 0


def pl_text_delete(text_id):
    rec = _recorder()
    if rec is None:
        return 0
    obj = rec._live("text", text_id)
    obj["deleted"] = rec._created()
    return 0


def pl_arw_new(d, t, p, direction):
    """Create an arrow. The fourth argument is MultiCharts' down-flag."""
    rec = _recorder()
    if rec is None:
        return _legacy_id()
    return rec.arw_new(d, t, p, _as_bool(direction))


def pl_tl_new_s(d1, t1, p1, d2, t2, p2):
    """Create a trendline; times are HHMMSS."""
    return pl_tl_new(d1, int(t1) // 100, p1, d2, int(t2) // 100, p2)


def pl_tl_setbegin(tl_id, d, t, p):
    rec = _recorder()
    if rec is None:
        return 0
    obj = rec._live("tl", tl_id)
    obj["anchors"][0] = rec._anchor(d, t, p)
    return 0


def pl_tl_setend(tl_id, d, t, p):
    rec = _recorder()
    if rec is None:
        return 0
    obj = rec._live("tl", tl_id)
    obj["anchors"][1] = rec._anchor(d, t, p)
    return 0


def pl_tl_setextleft(tl_id, val):
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_set(tl_id, "ext_left", _as_bool(val))


def pl_tl_setextright(tl_id, val):
    rec = _recorder()
    if rec is None:
        return 0
    return rec.tl_set(tl_id, "ext_right", _as_bool(val))


def _live_obj(kind, oid):
    rec = _recorder()
    if rec is None:
        raise DrawingGetterError(
            "a drawing getter needs drawings=True on the run; "
            "refusing to return 0")
    return rec._live(kind, oid)


def _anchor_field(kind, oid, index, field):
    return _live_obj(kind, oid)["anchors"][index][field]


def _unmeasured(detail):
    raise DrawingGetterError(detail)


def pl_tl_getcolor(tl_id):
    return _live_obj("tl", tl_id)["color"]


def pl_tl_getstyle(tl_id):
    return _live_obj("tl", tl_id)["style"]


def pl_tl_getsize(tl_id):
    return _live_obj("tl", tl_id)["size"]


def pl_tl_getextleft(tl_id):
    return _live_obj("tl", tl_id)["ext_left"]


def pl_tl_getextright(tl_id):
    return _live_obj("tl", tl_id)["ext_right"]


def pl_tl_getbegindate(tl_id):
    return _anchor_field("tl", tl_id, 0, "date")


def pl_tl_getbegintime(tl_id):
    return _anchor_field("tl", tl_id, 0, "time")


def pl_tl_getbegintime_s(tl_id):
    return _anchor_field("tl", tl_id, 0, "time_s")


def pl_tl_getbeginval(tl_id):
    return _anchor_field("tl", tl_id, 0, "price")


def pl_tl_getenddate(tl_id):
    return _anchor_field("tl", tl_id, 1, "date")


def pl_tl_getendtime(tl_id):
    return _anchor_field("tl", tl_id, 1, "time")


def pl_tl_getendtime_s(tl_id):
    return _anchor_field("tl", tl_id, 1, "time_s")


def pl_tl_getendval(tl_id):
    return _anchor_field("tl", tl_id, 1, "price")


def pl_tl_getbeginbar(tl_id):
    _unmeasured(
        "TL_GetBeginBar is not measured; the dump's bn is not "
        "MultiCharts CurrentBar, so this refuses to return a bar number")


def pl_tl_getendbar(tl_id):
    _unmeasured(
        "TL_GetEndBar is not measured; the dump's bn is not "
        "MultiCharts CurrentBar, so this refuses to return a bar number")


def pl_tl_getbegin_bn(tl_id):
    _unmeasured("TL_GetBegin_BN is not measured; refusing to return 0")


def pl_tl_getbegin_dt(tl_id):
    _unmeasured("TL_GetBegin_Dt is not measured; refusing to return 0")


def pl_tl_getend_bn(tl_id):
    _unmeasured("TL_GetEnd_BN is not measured; refusing to return 0")


def pl_tl_getend_dt(tl_id):
    _unmeasured("TL_GetEnd_Dt is not measured; refusing to return 0")


def pl_tl_getvalue(tl_id, date, time):
    _unmeasured(
        "TL_GetValue interpolation has no MultiCharts measurement in the "
        "golden corpus; refusing to return 0")


def pl_tl_getvalue_bn(tl_id, bar):
    _unmeasured(
        "TL_GetValue_BN interpolation is not measured; refusing to return 0")


def pl_tl_getvalue_dt(tl_id, dt):
    _unmeasured(
        "TL_GetValue_Dt interpolation is not measured; refusing to return 0")


def pl_tl_getvalue_s(tl_id, date, time_s):
    _unmeasured(
        "TL_GetValue_s interpolation is not measured; refusing to return 0")


def pl_text_getstring(text_id):
    return _live_obj("text", text_id)["text"]


def pl_text_getcolor(text_id):
    return _live_obj("text", text_id)["color"]


def pl_text_getbgcolor(text_id):
    return _live_obj("text", text_id)["bgcolor"]


def pl_text_getborder(text_id):
    return _live_obj("text", text_id)["border"]


def pl_text_getfontname(text_id):
    return _live_obj("text", text_id)["font"]


def pl_text_gethstyle(text_id):
    return _live_obj("text", text_id)["hstyle"]


def pl_text_getvstyle(text_id):
    return _live_obj("text", text_id)["vstyle"]


def pl_text_getdate(text_id):
    return _anchor_field("text", text_id, 0, "date")


def pl_text_gettime(text_id):
    return _anchor_field("text", text_id, 0, "time")


def pl_text_gettime_s(text_id):
    return _anchor_field("text", text_id, 0, "time_s")


def pl_text_getvalue(text_id):
    return _anchor_field("text", text_id, 0, "price")


def pl_arw_getcolor(arw_id):
    return _live_obj("arw", arw_id)["color"]


def pl_arw_getstyle(arw_id):
    return _live_obj("arw", arw_id)["style"]


def pl_arw_getsize(arw_id):
    return _live_obj("arw", arw_id)["size"]


def pl_arw_gettext(arw_id):
    return _live_obj("arw", arw_id)["text"]


def pl_arw_getdirection(arw_id):
    """The down-flag Arw_New was given (true = down)."""
    return _live_obj("arw", arw_id)["direction"] == "down"


def pl_arw_getdate(arw_id):
    return _anchor_field("arw", arw_id, 0, "date")


def pl_arw_gettime(arw_id):
    return _anchor_field("arw", arw_id, 0, "time")


def pl_arw_gettime_s(arw_id):
    return _anchor_field("arw", arw_id, 0, "time_s")


def pl_arw_getval(arw_id):
    return _anchor_field("arw", arw_id, 0, "price")


def pl_text_new_s(d, t, p, s):
    """Create text; the time is HHMMSS."""
    return pl_text_new(d, int(t) // 100, p, s)


def pl_text_setstyle(text_id, h_style, v_style):
    rec = _recorder()
    if rec is None:
        return 0
    obj = rec._live("text", text_id)
    obj["hstyle"] = int(h_style)
    obj["vstyle"] = int(v_style)
    return 0


def pl_text_setlocation(text_id, d, t, p):
    rec = _recorder()
    if rec is None:
        return 0
    obj = rec._live("text", text_id)
    obj["anchors"][0] = rec._anchor(d, t, p)
    return 0


def pl_text_setfontname(text_id, name):
    rec = _recorder()
    if rec is None:
        return 0
    rec._live("text", text_id)["font"] = str(name)
    return 0


def pl_text_setfontsize(text_id, size):
    rec = _recorder()
    if rec is None:
        return 0
    rec._live("text", text_id)["font_size"] = int(size)
    return 0


def pl_arw_new_s(d, t, p, direction):
    """Create an arrow; the time is HHMMSS."""
    return pl_arw_new(d, int(t) // 100, p, direction)


def make_dump(recorder, dates, times, *, case_id, tape_name, tape_sha256,
              tick, producer_kind):
    """Build a validated v1 dump from a finished recorder."""
    from .drawing_dump import build_dump
    n_bars = len(dates)
    if n_bars < 1:
        raise DrawingError("cannot build a drawing dump for an empty tape")
    return build_dump(
        recorder.objects,
        case_id=case_id,
        tape_name=tape_name,
        tape_sha256=tape_sha256,
        n_bars=n_bars,
        first=(dates[0], times[0]),
        last=(dates[-1], times[-1]),
        tick=tick,
        producer_kind=producer_kind,
    )


def resolve_drawing_request(case_id, tape, default_tick):
    """Return ``(case_id, name, sha256, tick)`` or raise if the request is incomplete."""
    if not case_id:
        raise ValueError("drawings=True requires drawing_case_id")
    if not isinstance(tape, dict) or "name" not in tape or "sha256" not in tape:
        raise ValueError(
            "drawings=True requires drawing_tape={'name', 'sha256'} "
            "(the tape identity the dump is compared against)")
    tick = float(tape.get("tick", default_tick))
    return case_id, str(tape["name"]), str(tape["sha256"]), tick
