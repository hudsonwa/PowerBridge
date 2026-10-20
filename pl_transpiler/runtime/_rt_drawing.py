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

"""PowerLanguage runtime: Trendline, text and arrow drawing stubs (and the shared drawing id counter).

Re-exported by pl_runtime; see that module."""

# ---- Drawing stubs ----

_drawing_id_counter = 0


def pl_tl_new(d1, t1, p1, d2, t2, p2):
    """Create a trendline, return ID."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_tl_setcolor(tl_id, color):
    pass


def pl_tl_setstyle(tl_id, style):
    pass


def pl_tl_setwidth(tl_id, width):
    pass


def pl_tl_delete(tl_id):
    pass


def pl_text_new(d, t, p, s):
    """Create a text object, return ID."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_text_setstring(text_id, s):
    pass


def pl_text_setcolor(text_id, color):
    pass


def pl_text_delete(text_id):
    pass


def pl_arw_new(d, t, p, direction):
    """Create an arrow object, return ID."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


# ---- Drawing Extension Stubs ----

def pl_tl_new_s(d1, t1, p1, d2, t2, p2):
    """Create trendline with seconds time."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_tl_setbegin(tl_id, d, t, p):
    pass


def pl_tl_setend(tl_id, d, t, p):
    pass


def pl_tl_setextleft(tl_id, val):
    pass


def pl_tl_setextright(tl_id, val):
    pass


def pl_tl_getbeginbar(tl_id):
    return 0


def pl_tl_getendbar(tl_id):
    return 0


def pl_tl_getbeginval(tl_id):
    return 0.0


def pl_tl_getendval(tl_id):
    return 0.0


def pl_text_new_s(d, t, p, s):
    """Create text with seconds time."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter


def pl_text_setstyle(text_id, h_style, v_style):
    pass


def pl_text_setlocation(text_id, d, t, p):
    pass


def pl_text_getstring(text_id):
    return ""


def pl_text_setfontname(text_id, name):
    pass


def pl_text_setfontsize(text_id, size):
    pass


def pl_arw_new_s(d, t, p, direction):
    """Create arrow with seconds time."""
    global _drawing_id_counter
    _drawing_id_counter += 1
    return _drawing_id_counter
