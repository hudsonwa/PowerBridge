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

"""PowerLanguage runtime: DateTime conversion helpers and the remaining PowerLanguage builtins.

Re-exported by pl_runtime; see that module."""

from ._rt_core import f32
from ._rt_indicators import pl_roc

# --- DateTime helpers (GTA6 date/time conversion family) --------------------
# PowerLanguage uses two grounded numeric encodings here:
#   * "DateTime serial": int part = days since 1899-12-30 (OLE/Excel style),
#     frac = fraction of day. EncodeDate/StringToDate/IncMonth use this
#     (pdf:4558 EncodeDate(08,01,01)=39448; pdf:4704 IncMonth(39417,1)=39448).
#   * The runner's per-bar `datetime` value is YYYYMMDD.frac (run_gt builds it
#     from Date+Time). DateTime2ELTime/DayOfWeek/*ToString/FormatDate/FormatTime
#     consume that form. Both share the same fractional-day convention.
def _ole_from_ymd(yyyy, mm, dd):
    import datetime as _dt
    return (_dt.date(int(yyyy), int(mm), int(dd)) - _dt.date(1899, 12, 30)).days

def _ymd_from_ole(serial):
    import datetime as _dt
    d = _dt.date(1899, 12, 30) + _dt.timedelta(days=int(round(float(serial))))
    return d.year, d.month, d.day

def _decode_dt_yyyymmdd(dt):
    """Decode a DateTime into (y,mo,d,h,mi,s).

    Accepts the runner's YYYYMMDD.frac bar DateTime and the MultiCharts serial
    DateTime (days since 1899-12-30, what ELDateToDateTime/DateToJulian return).
    A serial value is always < 10,000,000; a YYYYMMDD value never is."""
    dt = float(dt)
    ip = int(dt)
    frac = dt - ip
    tot = int(round(frac * 86400.0))
    if -693594 < ip < 10000000:
        import datetime as _dtm
        day = _dtm.date(1899, 12, 30) + _dtm.timedelta(days=ip)
        return day.year, day.month, day.day, tot // 3600, (tot % 3600) // 60, tot % 60
    return ip // 10000, (ip // 100) % 100, ip % 100, tot // 3600, (tot % 3600) // 60, tot % 60

def _fmt_dt(fmt, y, mo, d, h, mi, s, ms):
    import re as _re, calendar as _cal
    h12 = h % 12 or 12
    tt = 'AM' if h < 12 else 'PM'
    wd = _cal.weekday(y, mo, d)  # Mon=0..Sun=6
    full = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    tokens = {
        'dddd': full[wd], 'ddd': full[wd][:3], 'dd': f"{d:02d}", 'd': str(d),
        'MMMM': _cal.month_name[mo], 'MMM': _cal.month_abbr[mo],
        'MM': f"{mo:02d}", 'M': str(mo),
        'yyyy': f"{y:04d}", 'yy': f"{y % 100:02d}", 'y': str(y % 100),
        'HH': f"{h:02d}", 'H': str(h), 'hh': f"{h12:02d}", 'h': str(h12),
        'mm': f"{mi:02d}", 'm': str(mi), 'ss': f"{s:02d}", 's': str(s),
        'fff': f"{ms:03d}", 'tt': tt, 't': tt[0],
    }
    pat = _re.compile('|'.join(sorted((_re.escape(k) for k in tokens), key=len, reverse=True)))
    return pat.sub(lambda m: tokens[m.group(0)], fmt)

def _parse_time_frac(s):
    """Parse 'hh:mm:ss tt' (or 24h) -> fraction of day."""
    import re as _re
    s = str(s).strip()
    ampm = None
    m = _re.search(r'(?i)\b(AM|PM)\b', s)
    if m:
        ampm = m.group(1).upper()
        s = s[:m.start()].strip()
    parts = [int(x) for x in s.split(':') if x != '']
    hh = parts[0] if parts else 0
    mi = parts[1] if len(parts) > 1 else 0
    ss = parts[2] if len(parts) > 2 else 0
    if ampm == 'PM' and hh < 12:
        hh += 12
    if ampm == 'AM' and hh == 12:
        hh = 0
    return (hh * 3600 + mi * 60 + ss) / 86400.0

def _parse_dt_formatted(value, fmt):
    """StringToDTFormatted: parse `value` using a MC format string -> serial(+frac)."""
    import re as _re
    field_pat = {
        'yyyy': (r'\d{4}', 'y4'), 'yy': (r'\d{2}', 'y2'),
        'MM': (r'\d{1,2}', 'M'), 'M': (r'\d{1,2}', 'M'),
        'dd': (r'\d{1,2}', 'd'), 'd': (r'\d{1,2}', 'd'),
        'HH': (r'\d{1,2}', 'H'), 'H': (r'\d{1,2}', 'H'),
        'hh': (r'\d{1,2}', 'h'), 'h': (r'\d{1,2}', 'h'),
        'mm': (r'\d{1,2}', 'mi'), 'm': (r'\d{1,2}', 'mi'),
        'ss': (r'\d{1,2}', 's'), 's': (r'\d{1,2}', 's'),
        'tt': (r'[APap][Mm]', 'ap'), 't': (r'[APap]', 'ap'),
    }
    toks = sorted(field_pat, key=len, reverse=True)
    regex, fields, i = '', [], 0
    while i < len(fmt):
        for t in toks:
            if fmt.startswith(t, i):
                pat, field = field_pat[t]
                regex += '(' + pat + ')'
                fields.append(field)
                i += len(t)
                break
        else:
            regex += _re.escape(fmt[i])
            i += 1
    m = _re.match(regex, str(value).strip())
    if not m:
        return 0
    vals = {f: m.group(idx + 1) for idx, f in enumerate(fields)}
    if 'y4' in vals:
        y = int(vals['y4'])
    elif 'y2' in vals:
        y = int(vals['y2'])
        y = 1900 + y if y >= 50 else 2000 + y
    else:
        y = 1900
    mo = int(vals.get('M', 1))
    d = int(vals.get('d', 1))
    h = int(vals.get('H', vals.get('h', 0)))
    mi = int(vals.get('mi', 0))
    s = int(vals.get('s', 0))
    ap = vals.get('ap', '').upper()
    if ap.startswith('P') and h < 12:
        h += 12
    if ap.startswith('A') and h == 12:
        h = 0
    serial = _ole_from_ymd(y, mo, d)
    frac = (h * 3600 + mi * 60 + s) / 86400.0
    return serial + frac if frac else float(serial)

def pl_datetime2eltime(*args, **kwargs):
    # DateTime2ELTime(DateTime) -> HHmm (pdf:4327 39449.65625 -> 1545).
    if not args:
        return 0
    _y, _mo, _d, h, mi, _s = _decode_dt_yyyymmdd(args[0])
    return h * 100 + mi

def pl_datetime2eltime_s(*args, **kwargs):
    # DateTime2ELTime_s(DateTime) -> HHmmss (pdf:4342 39449.646354167 -> 153045).
    if not args:
        return 0
    _y, _mo, _d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return h * 10000 + mi * 100 + s

def pl_datetime_bar_update(*args, **kwargs):
    return 0

def pl_datetimetostring(*args, **kwargs):
    # DateTimeToString(DateTime) -> regional date+time string (pdf:4355 notes the
    # format is Regional-Options controlled). Capture locale is d/MM/yyyy h:mm:ss
    # tt; capture row1 = '2/01/2024 9:21:00 AM'.
    if not args:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('d/MM/yyyy h:mm:ss tt', y, mo, d, h, mi, s, 0)

def pl_datetimetostring_ms(*args, **kwargs):
    # DateTimeToString_Ms(DateTime) -> regional date+time with ms (pdf:4368).
    # Capture row1 = '2/01/2024 09:21:00.000'.
    if not args:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('d/MM/yyyy HH:mm:ss.fff', y, mo, d, h, mi, s, 0)

def pl_datetostring(*args, **kwargs):
    # DateToString(DateTime) -> regional date string (pdf:4402; Regional-Options
    # controlled). Capture locale d/MM/yyyy; capture row1 = '2/01/2024'.
    if not args:
        return ''
    y, mo, d, _h, _mi, _s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('d/MM/yyyy', y, mo, d, 0, 0, 0, 0)

def pl_timetostring(*args, **kwargs):
    # TimeToString(DateTime) -> regional time string (pdf:5059; Regional-Options
    # controlled). Capture locale h:mm:ss tt; capture row1 = '9:21:00 AM'.
    if not args:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[0])
    return _fmt_dt('h:mm:ss tt', y, mo, d, h, mi, s, 0)

def pl_datetojulian(*args, **kwargs):
    if not args:
        return 0
    el_date = int(args[0])
    yy = el_date // 10000
    mm = (el_date // 100) % 100
    dd = el_date % 100
    yyyy = 1900 + yy
    a = (14 - mm) // 12
    y = yyyy + 4800 - a
    m = mm + 12 * a - 3
    jdn = dd + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    # MC DateToJulian returns days since Dec 30, 1899 (Excel-style serial date)
    return jdn - 2415019

def pl_dayfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    date_part = int(dt)  # YYYYMMDD
    return date_part % 100  # DD

def pl_dayofweekfromdatetime(*args, **kwargs):
    # DayOfWeekFromDateTime(DateTime) -> Sunday=0..Saturday=6 (pdf:4465 Jan 1
    # 2008 -> 2, Tuesday).
    if not args:
        return 0
    import calendar as _cal
    y, mo, d, _h, _mi, _s = _decode_dt_yyyymmdd(args[0])
    return (_cal.weekday(y, mo, d) + 1) % 7

def pl_dom_askprice(*args, **kwargs):
    return 0

def pl_dom_askscount(*args, **kwargs):
    return 0

def pl_dom_asksize(*args, **kwargs):
    return 0

def pl_dom_bidprice(*args, **kwargs):
    return 0

def pl_dom_bidscount(*args, **kwargs):
    return 0

def pl_dom_bidsize(*args, **kwargs):
    return 0

def pl_dom_isconnected(*args, **kwargs):
    return 0

def pl_encodedate(*args, **kwargs):
    # EncodeDate(yy,MM,dd) -> DateTime serial, days since 1899-12-30 (pdf:4558
    # EncodeDate(08,01,01)=39448, Jan 1 2008; two-digit year).
    if len(args) < 3:
        return 0
    yy, mm, dd = int(args[0]), int(args[1]), int(args[2])
    if yy < 100:
        yy = 1900 + yy if yy >= 50 else 2000 + yy
    return float(_ole_from_ymd(yy, mm, dd))

def pl_encodetime(*args, **kwargs):
    # EncodeTime(HH,mm,ss,mmm) -> fraction of day (pdf:4574
    # EncodeTime(16,29,55,500)=0.6874479167).
    a = list(args) + [0, 0, 0, 0]
    hh, mm, ss, ms = int(a[0]), int(a[1]), int(a[2]), int(a[3])
    return (hh * 3600 + mm * 60 + ss + ms / 1000.0) / 86400.0

def pl_exchlisted(*args, **kwargs):
    return 0

def pl_execoffset(*args, **kwargs):
    return 0

def pl_expirationdate(*args, **kwargs):
    return 0

def pl_expirationdatefromvendor(*args, **kwargs):
    return 0

def pl_fileappend(*args, **kwargs):
    return 0

def pl_filedelete(*args, **kwargs):
    return 0

def pl_fill_array(*args, **kwargs):
    # Fill_Array(ArrayName, Value): set every element of the array to Value,
    # recursing into nested lists so multi-dimensional arrays fill too.
    if len(args) < 2 or not isinstance(args[0], list):
        return 0
    value = args[1]

    def _fill(arr):
        for i, item in enumerate(arr):
            if isinstance(item, list):
                _fill(item)
            else:
                arr[i] = value
    _fill(args[0])
    return 0

def pl_formatdate(*args, **kwargs):
    # FormatDate("FormatString", DateTime) -> formatted date string (pdf:4593).
    if len(args) < 2:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[1])
    return _fmt_dt(str(args[0]), y, mo, d, h, mi, s, 0)

def pl_formattime(*args, **kwargs):
    # FormatTime("FormatString", DateTime) -> formatted time string (pdf:4644).
    if len(args) < 2:
        return ''
    y, mo, d, h, mi, s = _decode_dt_yyyymmdd(args[1])
    return _fmt_dt(str(args[0]), y, mo, d, h, mi, s, 0)

def pl_getaccount(*args, **kwargs):
    return 0

def pl_getaccountid(*args, **kwargs):
    return 0

def pl_getappinfo(*args, **kwargs):
    return 0

def pl_getbackgroundcolor(*args, **kwargs):
    return 0

def pl_getbvalue(*args, **kwargs):
    return 0

def pl_getcdromdrive(*args, **kwargs):
    return 0

def pl_getcountry(*args, **kwargs):
    return 0

def pl_getcurrency(*args, **kwargs):
    return 0

def pl_getexchangename(*args, **kwargs):
    return 0

def pl_getgvalue(*args, **kwargs):
    return 0

def pl_getnumaccounts(*args, **kwargs):
    return 0

def pl_getnumpositions(*args, **kwargs):
    return 0

def pl_getplotbgcolor(*args, **kwargs):
    return 0

def pl_getplotcolor(*args, **kwargs):
    return 0

def pl_getplotwidth(*args, **kwargs):
    return 0

def pl_getpositionaverageprice(*args, **kwargs):
    return 0

def pl_getpositionopenpl(*args, **kwargs):
    return 0

def pl_getpositionquantity(*args, **kwargs):
    return 0

def pl_getpositionsymbol(*args, **kwargs):
    return 0

def pl_getpositiontotalcost(*args, **kwargs):
    return 0

def pl_getrtaccountequity(*args, **kwargs):
    return 0

def pl_getrtaccountnetworth(*args, **kwargs):
    return 0

def pl_getrtsymbolname(*args, **kwargs):
    return 0

def pl_getrtunrealizedpl(*args, **kwargs):
    return 0

def pl_getrvalue(*args, **kwargs):
    return 0

def pl_getstrategyname(*args, **kwargs):
    return 0

def pl_getsymbolname(*args, **kwargs):
    return 0

def pl_getuserid(*args, **kwargs):
    return 0

def pl_getusername(*args, **kwargs):
    return 0

def pl_gradientcolor(*args, **kwargs):
    return 0

def pl_hoursfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    total_seconds = int(round(frac * 86400))
    hours = total_seconds // 3600
    return hours

def pl_i_closedequity(*args, **kwargs):
    return 0

def pl_i_currentcontracts(*args, **kwargs):
    return 0

def pl_i_currentshares(*args, **kwargs):
    return 0

def pl_i_getplotvalue(*args, **kwargs):
    return 0

def pl_i_openequity(*args, **kwargs):
    return 0

def pl_i_setplotvalue(*args, **kwargs):
    return 0

def pl_incmonth(*args, **kwargs):
    # IncMonth(JulianDate, M): add M calendar months to a DateTime serial
    # (pdf:4704 IncMonth(39417,1)=39448). MultiCharts does NOT clamp the day to
    # the target month's length: it keeps the day-of-month and lets an overflow
    # roll forward into the following month. Verified against the GTA6 capture:
    #   IncMonth(Jan 30 2024, +1) -> "Feb 30" -> Mar 1 2024 (Feb has 29 days)
    #   IncMonth(Jan 31 2024, +1) -> "Feb 31" -> Mar 2 2024
    # i.e. days past the target month-end spill into the next month rather than
    # being truncated to the last valid day.
    if len(args) < 2:
        return 0
    import datetime as _dt, calendar as _cal
    y, mo, d = _ymd_from_ole(args[0])
    total = y * 12 + (mo - 1) + int(args[1])
    ny, nmo = total // 12, total % 12 + 1
    month_len = _cal.monthrange(ny, nmo)[1]
    base = _dt.date(ny, nmo, min(d, month_len))
    overflow = d - month_len if d > month_len else 0
    return (base + _dt.timedelta(days=overflow) - _dt.date(1899, 12, 30)).days

def pl_initialcapital(*args, **kwargs):
    return 0

def pl_insideask(*args, **kwargs):
    return 0

def pl_insidebid(*args, **kwargs):
    return 0

def pl_instr(*args, **kwargs):
    if len(args) >= 2:
        try:
            # InStr(String1, String2): 1-based position of String2 in String1
            return args[0].find(args[1]) + 1
        except (ValueError, AttributeError):
            return 0
    return 0

def pl_intrabarordergeneration(*args, **kwargs):
    return 0

def pl_jpy(*args, **kwargs):
    return 0

def pl_juliantodate(*args, **kwargs):
    if not args:
        return 0
    mc_jd = int(args[0])
    jdn = mc_jd + 2415019
    f = jdn + 1401 + (((4 * jdn + 274277) // 146097) * 3) // 4 - 38
    e = 4 * f + 3
    g = (e % 1461) // 4
    h = 5 * g + 2
    dd = (h % 153) // 5 + 1
    mm = (h // 153 + 2) % 12 + 1
    yy = e // 1461 - 4716 + (14 - mm) // 12
    el_date = ((yy - 1900) * 10000) + (mm * 100) + dd
    return el_date

def pl_last(*args, **kwargs):
    return 0

# Computer-clock builtins. They read the local computer clock, never the bars,
# so a backtest result cannot depend on them unless the strategy uses them on
# purpose. Tests patch _clock_now to pin the time.
def _clock_now():
    import datetime as _dt
    return _dt.datetime.now()


def _ole_days(now):
    """Days since 1899-12-30 plus the time of day as a fraction (EL DateTime)."""
    import datetime as _dt
    delta = now - _dt.datetime(1899, 12, 30)
    return delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400.0


def pl_currentdate(*args, **kwargs):
    """CurrentDate: computer date as YYYMMDD (year - 1900)."""
    now = _clock_now()
    return (now.year - 1900) * 10000 + now.month * 100 + now.day


def pl_currentdate_s(*args, **kwargs):
    """CurrentDate_s: same value as CurrentDate (EL dates have no seconds part)."""
    return pl_currentdate()


def pl_currenttime(*args, **kwargs):
    """CurrentTime: computer time as HHMM."""
    now = _clock_now()
    return now.hour * 100 + now.minute


def pl_currenttime_s(*args, **kwargs):
    """CurrentTime_s: computer time as HHMMSS."""
    now = _clock_now()
    return now.hour * 10000 + now.minute * 100 + now.second


def pl_computerdatetime(*args, **kwargs):
    """ComputerDateTime: computer date and time as an EL DateTime double."""
    return _ole_days(_clock_now())


# LastCalc*: the time of the most recent calculation. Backtests pass these in
# from the final bar (bar_loop); called on their own they use the clock.
def pl_lastcalcdatetime(*args, **kwargs):
    return _ole_days(_clock_now())

def pl_lastcalcjdate(*args, **kwargs):
    return int(_ole_days(_clock_now()))

def _clock_secs():
    now = _clock_now()
    return now.hour * 3600 + now.minute * 60 + now.second

def pl_lastcalcmmtime(*args, **kwargs):
    return _clock_secs() // 60

def pl_lastcalcmstime(*args, **kwargs):
    now = _clock_now()
    return ((now.hour * 3600 + now.minute * 60 + now.second) * 1000
            + now.microsecond // 1000)

def pl_lastcalcsstime(*args, **kwargs):
    return _clock_secs()

def pl_legacycolortorgb(*args, **kwargs):
    return 0

def pl_legacycolorvalue(*args, **kwargs):
    return 0

def pl_lower(*args, **kwargs):
    return 0

def pl_lpbool(*args, **kwargs):
    return 0

def pl_lpbyte(*args, **kwargs):
    return 0

def pl_lpdouble(*args, **kwargs):
    return 0

def pl_lpdword(*args, **kwargs):
    return 0

def pl_lpfloat(*args, **kwargs):
    return 0

def pl_lpint(*args, **kwargs):
    return 0

def pl_lplong(*args, **kwargs):
    return 0

def pl_lpstr(*args, **kwargs):
    return 0

def pl_lpword(*args, **kwargs):
    return 0

def pl_margin(*args, **kwargs):
    return 0

def pl_maxbarsback(*args, **kwargs):
    # MaxBarsBack: the study's "Maximum Bars Back" setting (pdf:6483). Fixed chart/
    # study config for the GTA6 capture = 50 (cf. session-config constants).
    return 50

def pl_maxbarsforward(*args, **kwargs):
    # MaxBarsForward: chart right-margin size in bars (pdf:6496). Fixed chart config
    # for the GTA6 capture = 16.
    return 16

def pl_maxcontractsheld(*args, **kwargs):
    return 0

def pl_maxiddrawdown(*args, **kwargs):
    return 0

def pl_mc_arw_getactive(*args, **kwargs):
    return 0

def pl_mc_text_getactive(*args, **kwargs):
    return 0

def pl_mc_tl_getactive(*args, **kwargs):
    return 0

def pl_mc_tl_new(*args, **kwargs):
    return 0

def pl_mc_tl_new_bn(*args, **kwargs):
    return 0

def pl_mc_tl_new_dt(*args, **kwargs):
    return 0

def pl_mc_tl_new_self(*args, **kwargs):
    return 0

def pl_mc_tl_new_self_bn(*args, **kwargs):
    return 0

def pl_messagelog(*args, **kwargs):
    return 0

def pl_millisecondsfromdatetime(*args, **kwargs):
    # MillisecondsFromDateTime(DateTime) -> millisecond part (0-999) of the time of
    # day in a DateTime value: 16:29:55.500 (EncodeTime, pdf:4574 = .6874479167)
    # gives 500. Rounded to the nearest whole millisecond like *FromDateTime.
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    return int(round(frac * 86400000)) % 1000

def pl_minmove(*args, **kwargs):
    return 0

def pl_minutesfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    total_seconds = int(round(frac * 86400))
    hours = total_seconds // 3600
    minutes = (total_seconds - hours * 3600) // 60
    return minutes

def pl_monthfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    date_part = int(dt)  # YYYYMMDD
    return (date_part // 100) % 100  # MM

def pl_mouseclickbarnumber(*args, **kwargs):
    return 0

def pl_mouseclickctrlpressed(*args, **kwargs):
    return 0

def pl_mouseclickdatanumber(*args, **kwargs):
    return 0

def pl_mouseclickdatetime(*args, **kwargs):
    return 0

def pl_mouseclickprice(*args, **kwargs):
    return 0

def pl_mouseclickshiftpressed(*args, **kwargs):
    return 0

def pl_nthmaxlist(*args, **kwargs):
    if len(args) < 2:
        return 0
    n = int(args[0])
    values = list(args[1:])
    if n < 1 or n > len(values):
        return 0
    sorted_vals = sorted(values, reverse=True)
    return sorted_vals[n - 1]

def pl_nthminlist(*args, **kwargs):
    if len(args) < 2:
        return 0
    n = int(args[0])
    values = list(args[1:])
    if n < 1 or n > len(values):
        return 0
    sorted_vals = sorted(values)
    return sorted_vals[n - 1]

def pl_optiontype(*args, **kwargs):
    return 0

def pl_placemarketorder(*args, **kwargs):
    return 0

def pl_playsound(*args, **kwargs):
    return 0

def pl_plotpaintbar(*args, **kwargs):
    return 0

def pl_pmm_get_global_named_num(*args, **kwargs):
    return 0

def pl_pmm_get_global_named_str(*args, **kwargs):
    return 0

def pl_pmm_get_my_named_num(*args, **kwargs):
    return 0

def pl_pmm_get_my_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_global_named_num(*args, **kwargs):
    return 0

def pl_pmm_set_global_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_my_named_num(*args, **kwargs):
    return 0

def pl_pmm_set_my_named_str(*args, **kwargs):
    return 0

def pl_pmm_set_my_status(*args, **kwargs):
    return 0

def pl_pmms_get_strategy_named_num(*args, **kwargs):
    return 0

def pl_pmms_get_strategy_named_str(*args, **kwargs):
    return 0

def pl_pmms_set_strategy_named_num(*args, **kwargs):
    return 0

def pl_pmms_set_strategy_named_str(*args, **kwargs):
    return 0

def pl_pmms_strategies_allow_entries_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_deny_entries_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_get_by_symbol_name(*args, **kwargs):
    return 0

def pl_pmms_strategies_in_long_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_in_short_count(*args, **kwargs):
    return 0

def pl_pmms_strategies_pause_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_resume_all(*args, **kwargs):
    return 0

def pl_pmms_strategies_set_status_for_all(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_long_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_allow_short_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_currentcontracts(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_long_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_deny_short_entries(*args, **kwargs):
    return 0

def pl_pmms_strategy_is_paused(*args, **kwargs):
    return 0

def pl_pmms_strategy_pause(*args, **kwargs):
    return 0

def pl_pmms_strategy_resume(*args, **kwargs):
    return 0

def pl_pmms_strategy_set_status(*args, **kwargs):
    return 0

def pl_pmms_strategy_symbol(*args, **kwargs):
    return 0

def pl_pointvalue(*args, **kwargs):
    return 0

def pl_portfolio_currencycode(*args, **kwargs):
    return 0

def pl_portfolio_currententries(*args, **kwargs):
    return 0

def pl_portfolio_getmarginpercontract(*args, **kwargs):
    return 0

def pl_portfolio_investedcapital(*args, **kwargs):
    return 0

def pl_portfolio_maxiddrawdown(*args, **kwargs):
    return 0

def pl_portfolio_maxriskequityperpospercent(*args, **kwargs):
    return 0

def pl_portfolio_strategydrawdown(*args, **kwargs):
    return 0

def pl_portfolioentriespriority(*args, **kwargs):
    return 0

def pl_prevclose(*args, **kwargs):
    return 0

def pl_pricescale(*args, **kwargs):
    return 0

def pl_printer(*args, **kwargs):
    return 0

def pl_processmouseevents(*args, **kwargs):
    return 0

def pl_q_ask(*args, **kwargs):
    return 0

def pl_q_asksize(*args, **kwargs):
    return 0

def pl_q_bid(*args, **kwargs):
    return 0

def pl_q_bidsize(*args, **kwargs):
    return 0

def pl_q_bigpointvalue(*args, **kwargs):
    return 0

def pl_q_date(*args, **kwargs):
    return 0

def pl_q_exchangelisted(*args, **kwargs):
    return 0

def pl_q_last(*args, **kwargs):
    return 0

def pl_q_openinterest(*args, **kwargs):
    return 0

def pl_q_previousclose(*args, **kwargs):
    return 0

def pl_q_time(*args, **kwargs):
    return 0

def pl_q_time_dt(*args, **kwargs):
    return 0

def pl_q_time_s(*args, **kwargs):
    return 0

def pl_q_totalvolume(*args, **kwargs):
    return 0

def pl_raiseruntimeerror(*args, **kwargs):
    return 0

def pl_random(*args, **kwargs):
    return 0

def pl_recalclastbarafter(*args, **kwargs):
    return 0

def pl_recalcpersist(*args, **kwargs):
    return 0

def pl_regularsession(*args, **kwargs):
    return 0

def pl_revsize(*args, **kwargs):
    return 0

def pl_rgb(*args, **kwargs):
    return 0

def pl_rgbtolegacycolor(*args, **kwargs):
    return 0

def pl_sameexitfromoneentryonce(*args, **kwargs):
    return 0

def pl_scrolltobar(*args, **kwargs):
    return 0

def pl_secondsfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    frac = dt - int(dt)
    total_seconds = int(round(frac * 86400))
    hours = total_seconds // 3600
    minutes = (total_seconds - hours * 3600) // 60
    seconds = total_seconds - hours * 3600 - minutes * 60
    return seconds

def pl_sess1endtime(*args, **kwargs):
    # Sess1EndTime (backward-compat word, pdf:9083) — regular-session end time.
    # GT instrument session template (NQ 1-min capture): the traded session runs
    # 0830→1515 ET; the last bar of each day closes at 1515. Constant chart config.
    return 1515

def pl_sess1firstbartime(*args, **kwargs):
    # Sess1FirstBarTime (pdf:9087, example "returns 0945") — time of the first bar
    # of session 1. For this chart's 0830-open 1-min session the first bar closes at
    # 0831. Constant chart config.
    return 831

def pl_sess1starttime(*args, **kwargs):
    """Sess1StartTime: start time of the regular trading session (ET).
    For NQ / CME equities, the regular session starts at 08:30 ET (830 in EL HHmm format)."""
    return 830

def pl_sess2endtime(*args, **kwargs):
    # Sess2EndTime (backward-compat, pdf:9101). Single-session template → same end
    # time as session 1 (0830-1515 → 1515). Constant chart config.
    return 1515

def pl_sess2firstbartime(*args, **kwargs):
    # Sess2FirstBarTime (pdf:9105, example "returns 1725"). Single-session template →
    # same first-bar time as session 1 (0831). Constant chart config.
    return 831

def pl_sess2starttime(*args, **kwargs):
    # Sess2StartTime (backward-compat, pdf:9113). Session opens 0830 (first 1-min bar
    # closes at 0831). Constant chart config.
    return 830

def pl_sessioncount(*args, **kwargs):
    # SessionCount(SessionType) (pdf:9120) — number of sessions per week in the
    # template. This NQ chart uses the standard 5-day (Mon-Fri) week → 5 sessions.
    return 5

def pl_sessioncountms(*args, **kwargs):
    # SessionCountMS (pdf:9133) — money-management session count; same 5-day week.
    return 5

def pl_sessionendday(*args, **kwargs):
    # SessionEndDay(SessionType,SessionNum) (pdf:9147) — day-of-week the session ends.
    # The aligned ETH template's session 1 ends on day 1 (Monday).
    return 1

def pl_sessionenddayms(*args, **kwargs):
    # SessionEndDayMS(SessionNum) (pdf:9164) — same end day (1) for the MM session.
    return 1

def pl_sessionendtime(*args, **kwargs):
    """SessionEndTime: end time of the specified trading session and day.
    For NQ on a CME equity-index template (ETH+regular), session 1 (ETH) ends at 16:00 ET (1600)."""
    return 1600

def pl_sessionendtimems(*args, **kwargs):
    # SessionEndTimeMS(SessionNum) (pdf:9194) — MM/regular session end time. The
    # traded session ends 1515 (last bar of day). Constant chart config.
    return 1515

def pl_sessionlastbar(*args, **kwargs):
    # SessionLastBar (pdf:3775) → logical: True on the last bar of the trading
    # session. The runner derives this from the bar date series (next bar is a new
    # day) and passes it via the 'session_last_bar' kwarg, mirroring CurrentSession.
    # Fallback 0 when no runner override (e.g. live last-bar context).
    return kwargs.get('session_last_bar', 0)

def pl_sessionstartday(*args, **kwargs):
    # SessionStartDay(SessionType,SessionNum) (pdf:9206) — day-of-week the session
    # starts, 0 = Sunday. The aligned ETH template (SessionCount = 5) runs one
    # session per trading day, each opening the evening before: session 1 opens
    # Sunday (0) and ends Monday (SessionEndDay = 1), ..., session 5 opens Thursday.
    # A session number outside 1..SessionCount has no start day here: return 0.
    num = int(args[1]) if len(args) >= 2 else 1
    if 1 <= num <= pl_sessioncount():
        return num - 1
    return 0

def pl_sessionstartdayms(*args, **kwargs):
    # SessionStartDayMS(SessionNum) (pdf:9223) — MM session start day = 1 (Monday).
    return 1

def pl_sessionstarttime(*args, **kwargs):
    """SessionStartTime(SessionNumber, DayNumber): start time of the specified session and day.
    Returns time in HHmm format. For NQ on a CME aligned session template:
    session 1 (ETH) starts at 17:00 ET (1700)."""
    return 1700

def pl_currentsession(*args, **kwargs):
    """Return current trading session index.
    Default: 2 (the most common value for NQ aligned-session charts).
    Overridden by 'currentsession' kwarg from the runner."""
    return kwargs.get('currentsession', 2)

def eldatetodatetime(el_date):
    """Convert EL date (YYYYMMDD int) to datetime (stub).
    For chart/commentary use; not affect on trade logic."""
    import datetime
    if isinstance(el_date, (int, float)):
        y = int(el_date) // 10000
        m = (int(el_date) // 100) % 100
        d = int(el_date) % 100
        try:
            return datetime.datetime(y, m, d)
        except ValueError:
            return datetime.datetime(2020, 1, 1)
    return datetime.datetime(2020, 1, 1)

def pl_sessionstarttimems(*args, **kwargs):
    # SessionStartTimeMS(SessionNum) (pdf:9253) — MM/regular session start time.
    # Session opens 0830. Constant chart config.
    return 830

def pl_setcustomfitnessvalue(*args, **kwargs):
    return 0

def pl_setfpcompareaccuracy(*args, **kwargs):
    return 0

def pl_setmaxbarsback(*args, **kwargs):
    return 0

def pl_setstopcontract(*args, **kwargs):
    return 0

def pl_setstopshare(*args, **kwargs):
    return 0

def pl_settrailingstop_pt(*args, **kwargs):
    return 0

def pl_sine(*args, **kwargs):
    import math
    if args:
        return f32(math.sin(math.radians(float(args[0]))))
    return 0.0

def pl_slippage(*args, **kwargs):
    return 0

def pl_squareroot(*args, **kwargs):
    import math
    if args:
        return f32(math.sqrt(float(args[0])))
    return 0.0

def pl_strike(*args, **kwargs):
    return 0

def pl_stringtodate(*args, **kwargs):
    # StringToDate("MM/dd/yyyy") -> DateTime serial integer (pdf:4873/4881
    # StringToDate("01/01/2008")=39448).
    if not args:
        return 0
    p = str(args[0]).strip().split('/')
    mm, dd, yy = int(p[0]), int(p[1]), int(p[2])
    if yy < 100:
        yy = 1900 + yy if yy >= 50 else 2000 + yy
    return float(_ole_from_ymd(yy, mm, dd))

def pl_stringtodatetime(*args, **kwargs):
    # StringToDateTime("MM/dd/yyyy hh:mm:ss tt") -> serial+frac (pdf:4904/4914
    # StringToDateTime("01/01/2008 08:00:00 AM")=39448.33333333).
    if not args:
        return 0
    datepart, _, timepart = str(args[0]).strip().partition(' ')
    return pl_stringtodate(datepart) + _parse_time_frac(timepart)

def pl_stringtodtformatted(*args, **kwargs):
    # StringToDTFormatted("DateTimeString","FormatString") -> serial(+frac)
    # (pdf:4940/4946). e.g. ("02/17/11","MM/dd/yy") -> 40591 (Feb 17 2011).
    if len(args) < 2:
        return 0
    return _parse_dt_formatted(str(args[0]), str(args[1]))

def pl_stringtotime(*args, **kwargs):
    # StringToTime("hh:mm:ss tt") -> fraction of day (pdf:5000/5009).
    if not args:
        return 0.0
    return _parse_time_frac(str(args[0]))

def pl_symbol(*args, **kwargs):
    # Symbol: instrument ticker string (unimplemented). The loud-stub installer
    # wraps this; _UNIMPL_STR_RETURNING makes the non-strict fallback yield ''.
    return 0

def pl_symbol_close(*args, **kwargs):
    return 0

def pl_symbol_date(*args, **kwargs):
    return 0

def pl_symbol_downticks(*args, **kwargs):
    return 0

def pl_symbol_high(*args, **kwargs):
    return 0

def pl_symbol_low(*args, **kwargs):
    return 0

def pl_symbol_open(*args, **kwargs):
    return 0

def pl_symbol_openint(*args, **kwargs):
    return 0

def pl_symbol_tickid(*args, **kwargs):
    return 0

def pl_symbol_ticks(*args, **kwargs):
    return 0

def pl_symbol_time(*args, **kwargs):
    return 0

def pl_symbol_time_s(*args, **kwargs):
    return 0

def pl_symbol_upticks(*args, **kwargs):
    return 0

def pl_symbol_volume(*args, **kwargs):
    return 0

def pl_symbolcurrencycode(*args, **kwargs):
    return 0

def pl_symbolname(*args, **kwargs):
    # SymbolName: instrument name string (unimplemented; loud-stub -> '').
    return 0

def pl_tangent(*args, **kwargs):
    import math
    if args:
        return f32(math.tan(math.radians(float(args[0]))))
    return 0.0

def pl_text_anchor_to_bars(*args, **kwargs):
    return 0

def pl_text_get_anchor_to_bars(*args, **kwargs):
    return 0

def pl_text_getactive(*args, **kwargs):
    return 0

def pl_text_getattribute(*args, **kwargs):
    return 0

def pl_text_getbarnumber(*args, **kwargs):
    return 0

def pl_text_getbgcolor(*args, **kwargs):
    return 0

def pl_text_getborder(*args, **kwargs):
    return 0

def pl_text_getcolor(*args, **kwargs):
    return 0

def pl_text_getdate(*args, **kwargs):
    return 0

def pl_text_getfirst(*args, **kwargs):
    return 0

def pl_text_getfontname(*args, **kwargs):
    return 0

def pl_text_gethstyle(*args, **kwargs):
    return 0

def pl_text_getlock(*args, **kwargs):
    return 0

def pl_text_getnext(*args, **kwargs):
    return 0

def pl_text_getsize(*args, **kwargs):
    return 0

def pl_text_gettime(*args, **kwargs):
    return 0

def pl_text_gettime_dt(*args, **kwargs):
    return 0

def pl_text_gettime_s(*args, **kwargs):
    return 0

def pl_text_getvalue(*args, **kwargs):
    return 0

def pl_text_getvstyle(*args, **kwargs):
    return 0

def pl_text_lock(*args, **kwargs):
    return 0

def pl_text_new_bn(*args, **kwargs):
    return 0

def pl_text_new_dt(*args, **kwargs):
    return 0

def pl_text_setattribute(*args, **kwargs):
    return 0

def pl_text_setbarnumber(*args, **kwargs):
    return 0

def pl_text_setbgcolor(*args, **kwargs):
    return 0

def pl_text_setborder(*args, **kwargs):
    return 0

def pl_text_setlocation_bn(*args, **kwargs):
    return 0

def pl_text_setlocation_dt(*args, **kwargs):
    return 0

def pl_text_setlocation_s(*args, **kwargs):
    return 0

def pl_text_setsize(*args, **kwargs):
    return 0

def pl_tickid(*args, **kwargs):
    return 0

def pl_time2time_s(*args, **kwargs):
    # Time2Time_s(HHmm) -> HHmmss (pdf:5045 1015 -> 101500).
    if not args:
        return 0
    return int(args[0]) * 100

def pl_time_s2time(*args, **kwargs):
    # Time_s2Time(HHmmss) -> HHmm, seconds truncated (pdf:5076 101520 -> 1015).
    if not args:
        return 0
    return int(args[0]) // 100

def pl_eltimetodatetime_s(*args, **kwargs):
    """ELTimeToDateTime_s(HHmmss) -> time-of-day fraction of a DateTime serial
    (pdf:4516 ELTimeToDateTime_s(101525)=0.427372685)."""
    if not args:
        return 0.0
    t = int(args[0])
    hh, mm, ss = t // 10000, (t // 100) % 100, t % 100
    return (hh * 3600 + mm * 60 + ss) / 86400.0

def pl_barstatus(*args, **kwargs):
    """BarStatus(DataN): bar-completeness status. On closed historical bars the
    last tick is always present, so EL returns 2 (closing tick of the bar)."""
    return 2

def pl_tl_anchor_to_bars(*args, **kwargs):
    return 0

def pl_tl_get_anchor_to_bars(*args, **kwargs):
    return 0

def pl_tl_getactive(*args, **kwargs):
    return 0

def pl_tl_getalert(*args, **kwargs):
    return 0

def pl_tl_getbegin_bn(*args, **kwargs):
    return 0

def pl_tl_getbegin_dt(*args, **kwargs):
    return 0

def pl_tl_getbegindate(*args, **kwargs):
    return 0

def pl_tl_getbegintime(*args, **kwargs):
    return 0

def pl_tl_getbegintime_s(*args, **kwargs):
    return 0

def pl_tl_getcolor(*args, **kwargs):
    return 0

def pl_tl_getend_bn(*args, **kwargs):
    return 0

def pl_tl_getend_dt(*args, **kwargs):
    return 0

def pl_tl_getenddate(*args, **kwargs):
    return 0

def pl_tl_getendtime(*args, **kwargs):
    return 0

def pl_tl_getendtime_s(*args, **kwargs):
    return 0

def pl_tl_getextleft(*args, **kwargs):
    return 0

def pl_tl_getextright(*args, **kwargs):
    return 0

def pl_tl_getfirst(*args, **kwargs):
    return 0

def pl_tl_getlock(*args, **kwargs):
    return 0

def pl_tl_getnext(*args, **kwargs):
    return 0

def pl_tl_getsize(*args, **kwargs):
    return 0

def pl_tl_getstyle(*args, **kwargs):
    return 0

def pl_tl_getvalue(*args, **kwargs):
    return 0

def pl_tl_getvalue_bn(*args, **kwargs):
    return 0

def pl_tl_getvalue_dt(*args, **kwargs):
    return 0

def pl_tl_getvalue_s(*args, **kwargs):
    return 0

def pl_tl_lock(*args, **kwargs):
    return 0

def pl_tl_new_bn(*args, **kwargs):
    return 0

def pl_tl_new_dt(*args, **kwargs):
    return 0

def pl_tl_setalert(*args, **kwargs):
    return 0

def pl_tl_setbegin_bn(*args, **kwargs):
    return 0

def pl_tl_setbegin_dt(*args, **kwargs):
    return 0

def pl_tl_setbegin_s(*args, **kwargs):
    return 0

def pl_tl_setend_bn(*args, **kwargs):
    return 0

def pl_tl_setend_dt(*args, **kwargs):
    return 0

def pl_tl_setend_s(*args, **kwargs):
    return 0

def pl_tl_setsize(*args, **kwargs):
    return 0

def pl_tool_dashed2(*args, **kwargs):
    return 0

def pl_tool_dashed3(*args, **kwargs):
    return 0

def pl_varsize(*args, **kwargs):
    return 0

def pl_varstartaddr(*args, **kwargs):
    return 0

def pl_yearfromdatetime(*args, **kwargs):
    if not args:
        return 0
    dt = float(args[0])
    date_part = int(dt)  # YYYYMMDD
    return date_part // 10000  # YYYY

def pl_yesterday(*args, **kwargs):
    return 0

def pl_file(path):
    """File handle constructor for Print output. Returns path string."""
    return path

def pl_rateofchange(series, length):
    """Rate of Change alias for pl_roc."""
    return pl_roc(series, length)
