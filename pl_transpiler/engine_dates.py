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


def _parse_el_date(date_str):
    """Parse DD/MM/YYYY -> EL integer date YYYMMDD.
    
    EL Date format = YYYMMDD = (year - 1900) * 10000 + month * 100 + day.
    E.g. 2024-01-02 -> 1240102.
    """
    parts = date_str.split('/')
    if len(parts) == 3:
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        return (y - 1900) * 10000 + m * 100 + d
    return int(date_str)


def _parse_el_time(time_str):
    """Parse HH:MM:SS -> EL integer time HHMM."""
    parts = time_str.split(':')
    if len(parts) >= 2:
        return int(parts[0]) * 100 + int(parts[1])
    return int(time_str)


def _el_to_ole(el_date, el_time):
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


def _el_date_to_day_of_week(el_date):
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
