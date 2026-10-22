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

"""PowerLanguage runtime: session and bar-status builtins.

Re-exported by pl_runtime; see that module."""


def pl_regularsession(*args, **kwargs):
    return 0

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

def pl_sessionstarttimems(*args, **kwargs):
    # SessionStartTimeMS(SessionNum) (pdf:9253) — MM/regular session start time.
    # Session opens 0830. Constant chart config.
    return 830

def pl_tickid(*args, **kwargs):
    # TickID matches MultiCharts backtests: 0 on every historical bar
    # (checked against a MultiCharts capture). Whitelisted in _rt_core so it
    # is a real builtin, not a loud stub.
    return 0

def pl_barstatus(*args, **kwargs):
    """BarStatus(DataN): bar-completeness status. On closed historical bars the
    last tick is always present, so EL returns 2 (closing tick of the bar)."""
    return 2
