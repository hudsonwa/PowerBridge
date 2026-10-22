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

"""PowerLanguage runtime: colour builtins (RGB, gradients, legacy palette).

Re-exported by pl_runtime; see that module."""

from ._rt_core import UnimplementedBuiltinError


class UnmeasuredColorError(UnimplementedBuiltinError):
    """A colour call the golden corpus did not measure. Always stops the run."""

    def __init__(self, detail):
        self.builtin = "color"
        NotImplementedError.__init__(self, detail)

def pl_getbackgroundcolor(*args, **kwargs):
    return 0

def pl_getbvalue(*args, **kwargs):
    return 0

def pl_getgvalue(*args, **kwargs):
    return 0

def pl_getplotbgcolor(*args, **kwargs):
    return 0

def pl_getplotcolor(*args, **kwargs):
    return 0

def pl_getplotwidth(*args, **kwargs):
    return 0

def pl_getrvalue(*args, **kwargs):
    return 0

def _mc_channel(value, where):
    """One RGB channel. Truncates toward 0. Outside 0..255 is unmeasured."""
    try:
        channel = int(value)
    except (TypeError, ValueError):
        raise UnmeasuredColorError(
            f"{where} channel {value!r} is not a number") from None
    if isinstance(value, float) and value != channel and abs(value - channel) > 1e-6:
        raise UnmeasuredColorError(
            f"{where} channel {value!r} is not an integer; "
            "fractional channels are not measured")
    if not 0 <= channel <= 255:
        raise UnmeasuredColorError(
            f"{where} channel {channel} is outside 0..255; "
            "MultiCharts behaviour there is not measured")
    return channel


def _split_mc_color(color):
    """R, G, B from an MC colour int. Values outside 24 bits are unmeasured."""
    try:
        packed = int(color)
    except (TypeError, ValueError):
        raise UnmeasuredColorError(f"colour {color!r} is not an integer") from None
    if not 0 <= packed <= 0xFFFFFF:
        raise UnmeasuredColorError(
            f"colour {packed} is outside the measured 24-bit range")
    return packed & 0xFF, (packed >> 8) & 0xFF, (packed >> 16) & 0xFF


def pl_rgb(red, green, blue):
    """RGB(r, g, b) = r + 256·g + 65536·b. RGB(10,20,30) = 1971210 (g-colour-001-hours)."""
    r = _mc_channel(red, "RGB")
    g = _mc_channel(green, "RGB")
    b = _mc_channel(blue, "RGB")
    return r + 256 * g + 65536 * b


def pl_gradientcolor(value, min_value, max_value, start, end):
    """GradientColor. Channel = start + trunc((end-start)·f), f=(Value-Min)/(Max-Min).

    GradientColor(5,0,10,Red,Blue) = 8323200 (g-colour-001-hours). Min = Max
    is unmeasured and raises.
    """
    try:
        span = float(max_value) - float(min_value)
        frac = (float(value) - float(min_value)) / span
    except (TypeError, ValueError, ZeroDivisionError):
        raise UnmeasuredColorError(
            "GradientColor with Min = Max is not measured; refusing to guess"
        ) from None
    if span == 0.0:
        raise UnmeasuredColorError(
            "GradientColor with Min = Max is not measured; refusing to guess")
    sr, sg, sb = _split_mc_color(start)
    er, eg, eb = _split_mc_color(end)

    def _chan(start_c, end_c):
        return start_c + int((end_c - start_c) * frac)

    return pl_rgb(_chan(sr, er), _chan(sg, eg), _chan(sb, eb))

# The only measured palette point. Other indexes stay an error (g-colour-001-hours).
_LEGACY_COLOR_TO_RGB = {14: 32896}


def pl_legacycolortorgb(index):
    """LegacyColorToRGB. Only index 14 is measured: 32896."""
    try:
        key = int(index)
    except (TypeError, ValueError):
        key = None
    if key is None or key not in _LEGACY_COLOR_TO_RGB or float(index) != key:
        raise UnmeasuredColorError(
            "LegacyColorToRGB(%r) is not measured; only LegacyColorToRGB(14) "
            "= 32896 (g-colour-001-hours). Refusing to guess a palette."
            % (index,))
    return _LEGACY_COLOR_TO_RGB[key]

def pl_legacycolorvalue(*args, **kwargs):
    return 0

def pl_rgbtolegacycolor(*args, **kwargs):
    return 0
