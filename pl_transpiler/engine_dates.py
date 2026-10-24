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

"""engine_dates.py -- EL date/time parsers and conversions used by the engine."""
import calendar
import functools
import re
from collections.abc import Iterable
from typing import Optional, Union

# Field orders for slash/dash dates. MultiCharts' ASCII export writes the date
# in the Windows regional format; the exports this project was built against
# are day-first (D/MM/YYYY), so that stays the default.
DATE_FORMATS = ('dmy', 'mdy', 'ymd')
DEFAULT_DATE_FORMAT = 'dmy'


def _check_ymd(date_str: str, y: int, m: int, d: int) -> int:
    if not 1 <= m <= 12:
        raise ValueError("bad date %r: month %d is not 1-12" % (date_str, m))
    if not 1900 <= y <= 2899:
        raise ValueError("bad date %r: year %d is out of range" % (date_str, y))
    if not 1 <= d <= calendar.monthrange(y, m)[1]:
        raise ValueError("bad date %r: day %d is not valid for %04d-%02d"
                         % (date_str, d, y, m))
    return (y - 1900) * 10000 + m * 100 + d


def infer_date_format(date_strs: Iterable[str]) -> Optional[str]:
    """The slash-date field order of a whole file, for ``date_format=None``.

    Looks at every ``A/B/YYYY`` (or ``A-B-YYYY``) date whose first field is
    not a 4-digit year: an ``A`` above 12 means day-first (``'dmy'``), a ``B``
    above 12 month-first (``'mdy'``). Returns None when there is nothing to
    decide (no such dates, or every one reads the same either way). Raises
    ValueError when both orders occur, or when no field is above 12 and the
    two orders give different dates, so the caller must pass the order.
    """
    first_big = second_big = ambiguous = None
    for date_str in date_strs:
        parts = re.split(r'[/-]', date_str)
        if len(parts) != 3 or len(parts[0].strip()) == 4:
            continue
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            continue  # parse_el_date reports it
        if a > 12 and first_big is None:
            first_big = date_str
        elif b > 12 and second_big is None:
            second_big = date_str
        elif a != b and ambiguous is None:
            ambiguous = date_str
    if first_big is not None and second_big is not None:
        raise ValueError("slash dates disagree on the field order (%r is day-first, "
                         "%r month-first); pass --date-format dmy or mdy"
                         % (first_big, second_big))
    if first_big is not None:
        return 'dmy'
    if second_big is not None:
        return 'mdy'
    if ambiguous is not None:
        raise ValueError("slash dates are ambiguous (no day above 12, e.g. %r); "
                         "pass --date-format mdy (M/D/YYYY) or dmy (D/M/YYYY)"
                         % ambiguous)
    return None


@functools.lru_cache(maxsize=4096)
def parse_el_date(date_str: str, date_format: Optional[str] = None) -> int:
    """Parse a bar date -> EL integer date YYYMMDD.

    EL Date format = YYYMMDD = (year - 1900) * 10000 + month * 100 + day.
    E.g. 2024-01-02 -> 1240102.

    Accepted forms:
      * EL integer, e.g. ``1240102``;
      * three fields separated by ``/`` or ``-``, read in ``date_format`` order
        (``dmy`` by default, ``mdy`` or ``ymd``). With the default, a 4-digit
        first field is unambiguous and is read as year-month-day.

    Raises ValueError naming the value for an invalid month, day or year. With
    the default order, a second field above 12 gets a hint to pass
    ``--date-format mdy``.
    """
    fmt = date_format or DEFAULT_DATE_FORMAT
    if fmt not in DATE_FORMATS:
        raise ValueError("unknown date format %r (want one of: %s)"
                         % (fmt, ", ".join(DATE_FORMATS)))
    parts = re.split(r'[/-]', date_str)
    if len(parts) == 3:
        try:
            a, b, c = (int(x) for x in parts)
        except ValueError:
            raise ValueError("bad date %r" % date_str) from None
        if fmt == 'ymd' or (date_format is None and len(parts[0].strip()) == 4):
            return _check_ymd(date_str, a, b, c)
        if fmt == 'mdy':
            return _check_ymd(date_str, c, a, b)
        if date_format is None and b > 12 and 1 <= a <= 12:
            raise ValueError("bad date %r: month %d is not 1-12; dates are read "
                             "day-first (D/M/YYYY) by default -- for month-first "
                             "data pass --date-format mdy" % (date_str, b))
        return _check_ymd(date_str, c, b, a)
    try:
        v = int(date_str)
    except ValueError:
        raise ValueError("bad date %r (want D/M/YYYY or an EL date like 1240102)"
                         % date_str) from None
    return _check_ymd(date_str, 1900 + v // 10000, (v // 100) % 100, v % 100)


def parse_el_time(time_str: str) -> int:
    """Parse HH:MM[:SS] or an EL integer HHMM -> EL integer time HHMM.

    EL bar times have minute resolution, so a non-zero seconds field is an
    error rather than being silently dropped. Hours must be 0-23 and minutes
    0-59.
    """
    parts = time_str.split(':')
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        raise ValueError("bad time %r (want HH:MM[:SS] or HHMM)" % time_str) from None
    if len(parts) == 1:
        h, mi = divmod(nums[0], 100)
    elif len(parts) in (2, 3):
        h, mi = nums[0], nums[1]
        if len(parts) == 3 and nums[2] != 0:
            raise ValueError("bad time %r: seconds are not supported (EL bar times "
                             "are HHMM); use minute bars" % time_str)
        if not 0 <= nums[1] <= 59:
            raise ValueError("bad time %r: minute %d is not 0-59" % (time_str, nums[1]))
    else:
        raise ValueError("bad time %r (want HH:MM[:SS] or HHMM)" % time_str)
    if nums[0] < 0 or not 0 <= h <= 23 or not 0 <= mi <= 59:
        raise ValueError("bad time %r: not a time of day 00:00-23:59" % time_str)
    return h * 100 + mi


def el_to_ole(el_date: Optional[float], el_time: Optional[float]) -> float:
    """Convert an EL date (YYMMDD) + EL time (HHMM) to a MultiCharts DateTime serial
    (OLE automation date = days since 1899-12-30 + fraction-of-day). This is the value
    PosTradeEntry/ExitDateTime, EntryDateTime, ExitDateTime emit (pdf:12085 et al).
    Returns 0.0 when date is unset."""
    import datetime as _dt
    d = int(el_date or 0)
    if d <= 0:
        return 0.0
    yr = 1900 + d // 10000
    mo = (d // 100) % 100
    dy = d % 100
    t = int(el_time or 0)
    hh = t // 100
    mn = t % 100
    try:
        days = (_dt.date(yr, mo, dy) - _dt.date(1899, 12, 30)).days
    except ValueError:
        return 0.0
    return days + (hh * 3600 + mn * 60) / 86400.0


def el_date_to_day_of_week(el_date: Union[float, str]) -> int:
    """Return day-of-week (1=Mon..5=Fri) from an EL date integer (YYMMDD).
    
    EL CurrentSession(1) returns the day-of-week session number.
    """
    import datetime
    d = int(el_date)
    yr = (d // 10000) + 1900
    mo = (d // 100) % 100
    dy = d % 100
    dt = datetime.date(yr, mo, dy)
    return dt.isoweekday()  # 1=Mon..7=Sun; EL session values are 1..5


# The old underscore names, kept for callers that imported them.
_parse_el_date = parse_el_date
_parse_el_time = parse_el_time
_el_to_ole = el_to_ole
_el_date_to_day_of_week = el_date_to_day_of_week
