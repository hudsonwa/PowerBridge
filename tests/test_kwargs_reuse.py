#!/usr/bin/env python3
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

"""
test_kwargs_reuse.py — run_bar_loop mutates one kwargs dict in place.

Proves the reuse path keeps the old precedence:
  engine defaults < per_bar_extra < position kwargs < explicit barnumber/_state/...
and that a hook-only key from bar N cannot leak into bar N+1 (the old extra_kwargs
dict was rebuilt every bar).

Stdlib only, no captures.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler.engine import FillEngine, run_bar_loop
from pl_transpiler.runtime.instrument_config import get_config

NQ = get_config('NQ')


def _run(n, extra_fn, recorder):
    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)

    def strategy(*_series, **kwargs):
        recorder.append(dict(kwargs))
        return {'orders': [], 'risk': {}}

    dates = [1240102] * n
    times = [930 + i for i in range(n)]
    run_bar_loop(
        strategy,
        n_bars=n,
        get_ohlcv=lambda i: (1.0, 1.1, 0.9, 1.05, 1.0),
        dates=dates,
        times=times,
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        per_bar_extra=extra_fn,
        strict=True,
    )
    return recorder


def test_hook_only_key_does_not_leak():
    seen = []

    def extra(i):
        if i == 0:
            return {'ticks': 7, 'len': 9}
        return {}

    _run(2, extra, seen)
    assert seen[0]['ticks'] == 7, seen[0].get('ticks')
    assert seen[0]['len'] == 9, seen[0].get('len')
    assert 'ticks' not in seen[1], 'hook-only ticks leaked into bar 1'
    # len is not an engine key (an Input named Len must keep its own default),
    # so a hook-provided len is hook-only and must not leak into the next bar.
    assert 'len' not in seen[1], f"omitted len leaked: {seen[1].get('len')}"
    print("  [ok] hook-only ticks/len do not leak into the next bar")


def test_extra_overrides_engine_date_and_len():
    seen = []

    def extra(i):
        return {'current_date': 1250101, 'len': 3, 'prec': 2, 'data2_close': 99.5}

    _run(1, extra, seen)
    kw = seen[0]
    assert kw['current_date'] == 1250101, kw['current_date']
    assert kw['len'] == 3 and kw['prec'] == 2
    assert kw['data2_close'] == 99.5
    assert kw['barnumber'] == 1
    assert kw['time_s'] == (930 + 0) * 100
    print("  [ok] extra overrides current_date/len/prec; explicit barnumber/time_s win")


def test_position_kwargs_present_and_get_kwargs_matches():
    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    fe.market_position = 1
    fe.contracts = 2
    fe.entry_price = 18000.25
    fe.net_profit = 40.0

    def strategy(*_series, **kwargs):
        snap = fe.get_kwargs()
        for k, v in snap.items():
            assert kwargs[k] == v, f"{k}: loop={kwargs[k]!r} get_kwargs={v!r}"
        assert kwargs['market_position'] == 1
        assert kwargs['current_contracts'] == 2
        return {'orders': [], 'risk': {}}

    run_bar_loop(
        strategy,
        n_bars=1,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102],
        times=[930],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    print("  [ok] fill_pos_kwargs matches get_kwargs")


def test_constants_from_config():
    seen = []
    _run(1, None, seen)
    kw = seen[0]
    assert kw['pointvalue'] == NQ.pointvalue
    assert kw['bigpointvalue'] == NQ.bigpointvalue
    assert kw['sess1starttime'] == NQ.sess1starttime
    assert kw['barinterval'] == NQ.barinterval
    assert kw['bartype'] == 1
    assert kw['_state'] == {}
    print("  [ok] config constants land in the reused kwargs dict")


def test_extra_override_pointvalue_does_not_leak():
    """Extra may override a config constant; omitting it next bar restores it."""
    seen = []

    def extra(i):
        if i == 0:
            return {'pointvalue': 99.0, 'len': 9}
        return {}

    _run(2, extra, seen)
    assert seen[0]['pointvalue'] == 99.0, seen[0]['pointvalue']
    assert seen[0]['len'] == 9
    assert seen[1]['pointvalue'] == NQ.pointvalue, (
        f"omitted pointvalue must restore config, got {seen[1]['pointvalue']}")
    assert 'len' not in seen[1], f"omitted len leaked: {seen[1].get('len')}"
    print("  [ok] extra pointvalue/len override does not leak into next bar")


def test_persist_path_computes_datetime_when_read():
    """8-arg persist strategy that names 'datetime' still gets YYYYMMDD.frac."""
    seen = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        seen.append(kwargs['datetime'])
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    run_bar_loop(
        strategy,
        n_bars=1,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102],
        times=[930],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    # 1240102 -> 2024-01-02; 09:30 -> 34200/86400
    want = 20240102 + (9 * 3600 + 30 * 60) / 86400.0
    assert seen == [want], (seen, want)
    print("  [ok] persist-path datetime is YYYYMMDD.frac when the strategy reads it")


def test_persist_path_unread_datetime_does_not_break_date():
    """8-arg persist strategy that never names datetime still gets current_date."""
    seen = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        seen.append((kwargs['current_date'], kwargs['current_time'],
                     kwargs['sess1starttime'], kwargs.get('len')))
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    run_bar_loop(
        strategy,
        n_bars=1,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102],
        times=[930],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert seen == [(1240102, 930, NQ.sess1starttime, None)], seen
    print("  [ok] persist-path unread datetime still supplies date/time/constants")


def test_persist_path_daily_ohlc_when_named():
    """8-arg persist that names OpenD/HighD/LowD/CloseD still gets daily OHLC."""
    seen = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        seen.append((
            kwargs['opend_0'], kwargs['highd_0'], kwargs['lowd_0'],
            kwargs['closed_0'], kwargs['closed_1'],
        ))
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    # Two days: 3 bars then 2 bars. Day-1 open=1, high=3, low=0.5, close last=2.
    # Day-2 open=10, high=12, low=9, close last=11. CloseD(1) on day 2 is 2.
    ohlcv = [
        (1.0, 1.5, 0.8, 1.2, 1.0),
        (1.2, 3.0, 0.5, 2.5, 1.0),
        (2.5, 2.6, 1.9, 2.0, 1.0),
        (10.0, 11.0, 9.5, 10.5, 1.0),
        (10.5, 12.0, 9.0, 11.0, 1.0),
    ]
    dates = [1240102, 1240102, 1240102, 1240103, 1240103]
    times = [930, 931, 932, 930, 931]
    run_bar_loop(
        strategy,
        n_bars=5,
        get_ohlcv=lambda i: ohlcv[i],
        dates=dates,
        times=times,
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert seen[0] == (1.0, 1.5, 0.8, 1.2, -1), seen[0]
    assert seen[1] == (1.0, 3.0, 0.5, 2.5, -1), seen[1]
    assert seen[2] == (1.0, 3.0, 0.5, 2.0, -1), seen[2]
    assert seen[3] == (10.0, 11.0, 9.5, 10.5, 2.0), seen[3]
    assert seen[4] == (10.0, 12.0, 9.0, 11.0, 2.0), seen[4]
    print("  [ok] persist-path daily OHLC when named")


def test_persist_path_session_last_bar_when_named():
    """8-arg persist that names session_last_bar gets 1 on the last bar of a day."""
    seen = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        seen.append(kwargs['session_last_bar'])
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    dates = [1240102, 1240102, 1240103]
    times = [930, 1600, 1600]
    run_bar_loop(
        strategy,
        n_bars=3,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=dates,
        times=times,
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert seen == [0, 1, 1], seen
    print("  [ok] persist-path session_last_bar when named")


def test_persist_path_unread_daily_still_writes_named_pos():
    """8-arg persist that never names daily OHLC still gets market_position."""
    seen = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        seen.append((kwargs['market_position'], kwargs['current_contracts']))
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    fe.market_position = 1
    fe.contracts = 2
    run_bar_loop(
        strategy,
        n_bars=1,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102],
        times=[930],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert seen == [(1, 2)], seen
    print("  [ok] persist-path unread daily OHLC still supplies named pos kwargs")


def test_starstar_kwargs_still_writes_daily_and_all_pos():
    """Hand-written **kwargs still gets daily OHLC and every position field."""
    seen = []

    def strategy(*_series, **kwargs):
        seen.append(kwargs)
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    fe.market_position = 1
    fe.contracts = 3
    fe.entry_price = 100.5
    run_bar_loop(
        strategy,
        n_bars=1,
        get_ohlcv=lambda i: (1.0, 1.1, 0.9, 1.05, 1.0),
        dates=[1240102],
        times=[930],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    kw = seen[0]
    assert kw['opend_0'] == 1.0, kw['opend_0']
    assert kw['highd_0'] == 1.1, kw['highd_0']
    assert kw['lowd_0'] == 0.9, kw['lowd_0']
    assert kw['closed_0'] == 1.05, kw['closed_0']
    assert kw['closed_1'] == -1, kw['closed_1']
    assert kw['session_last_bar'] in (0, 1)
    assert kw['currentsession'] == 2  # 1240102 is a Tuesday → EL session 2
    assert kw['current_date'] == 1240102, kw['current_date']
    assert kw['current_time'] == 930, kw['current_time']
    assert kw['barnumber'] == 1, kw['barnumber']
    assert kw['currentbar'] == 1, kw['currentbar']
    assert kw['outpath'] == '', kw['outpath']
    assert kw['_first_bar'] is True, kw['_first_bar']
    snap = fe.get_kwargs()
    for k, v in snap.items():
        assert kw[k] == v, f"{k}: loop={kw[k]!r} get_kwargs={v!r}"
    print("  [ok] **kwargs path still writes daily OHLC and all position fields")


def test_persist_path_barid_when_named():
    """8-arg persist that names bar-id keys still gets live values each bar."""
    seen = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        seen.append((
            kwargs['current_date'], kwargs['current_time'],
            kwargs['currentbar'], kwargs['barnumber'],
            kwargs['outpath'], kwargs['_first_bar'],
        ))
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    run_bar_loop(
        strategy,
        n_bars=2,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102, 1240103],
        times=[930, 931],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    assert seen == [
        (1240102, 930, 1, 1, '', True),
        (1240103, 931, 2, 2, '', False),
    ], seen
    print("  [ok] persist-path bar-id keys when named")


def test_persist_path_unread_barid_stays_at_init():
    """8-arg persist that never names bar-id keys leaves the init values.

    Inspect the reused kwargs dict from outside the strategy so the
    strategy's co_consts do not contain the skipped names (naming them
    would force the write). time_s is still written.
    """
    holder = []

    def strategy(o, h, l, c, v, d, t, kwargs=None, **_kw):
        if kwargs is None:
            kwargs = _kw
        if not holder:
            holder.append(kwargs)
        return {'orders': [], 'risk': {}}

    fe = FillEngine(config=NQ)
    fe.set_capture_stats(False)
    run_bar_loop(
        strategy,
        n_bars=2,
        get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
        dates=[1240102, 1240103],
        times=[930, 931],
        config=NQ,
        fill_engine=fe,
        emit_row=lambda *a: None,
        strict=True,
    )
    kw = holder[0]
    assert kw['current_date'] == 0, kw['current_date']
    assert kw['current_time'] == 0, kw['current_time']
    assert kw['currentbar'] == 0, kw['currentbar']
    assert kw['barnumber'] == 0, kw['barnumber']
    assert kw['outpath'] == '', kw['outpath']
    assert kw['_first_bar'] is True, kw['_first_bar']
    assert kw['time_s'] == 931 * 100, kw['time_s']
    print("  [ok] persist-path unread bar-id keys stay at init; time_s updates")


def test_emit_trace_default_injects_and_false_skips():
    """Default inject_trace writes position columns; emit_trace=False skips it.

    persist min-emit (pl_oracle) does not read the trace dict, so the skip must
    not change fills. Default True keeps run_el / GT CSV columns.
    """
    n = 3
    dates = [1240102] * n
    times = [930 + i for i in range(n)]

    def _count(emit_trace):
        fe = FillEngine(config=NQ)
        fe.set_capture_stats(False)
        n_inject = [0]
        orig = fe.inject_trace

        def wrapped(trace):
            n_inject[0] += 1
            orig(trace)

        fe.inject_trace = wrapped
        seen_keys = []

        def emit_row(_i, _bn, trace):
            seen_keys.append('_market_position' in trace)

        def strategy(*_series, **kwargs):
            return {'orders': [], 'risk': {}}

        run_bar_loop(
            strategy,
            n_bars=n,
            get_ohlcv=lambda i: (1.0, 1.0, 1.0, 1.0, 1.0),
            dates=dates,
            times=times,
            config=NQ,
            fill_engine=fe,
            emit_row=emit_row,
            strict=True,
            emit_trace=emit_trace,
        )
        return n_inject[0], seen_keys

    n_on, keys_on = _count(True)
    n_off, keys_off = _count(False)
    assert n_on == n, f"default inject_trace should run every bar, got {n_on}"
    assert keys_on == [True] * n, keys_on
    assert n_off == 0, f"emit_trace=False must skip inject_trace, got {n_off}"
    assert keys_off == [False] * n, keys_off
    print("  [ok] emit_trace=True injects; emit_trace=False skips")


def main():
    tests = [
        test_hook_only_key_does_not_leak,
        test_extra_overrides_engine_date_and_len,
        test_position_kwargs_present_and_get_kwargs_matches,
        test_constants_from_config,
        test_extra_override_pointvalue_does_not_leak,
        test_persist_path_computes_datetime_when_read,
        test_persist_path_unread_datetime_does_not_break_date,
        test_persist_path_daily_ohlc_when_named,
        test_persist_path_session_last_bar_when_named,
        test_persist_path_unread_daily_still_writes_named_pos,
        test_starstar_kwargs_still_writes_daily_and_all_pos,
        test_persist_path_barid_when_named,
        test_persist_path_unread_barid_stays_at_init,
        test_emit_trace_default_injects_and_false_skips,
    ]
    for t in tests:
        print(f"running {t.__name__} ...")
        t()
    print("\nALL KWARGS-REUSE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
