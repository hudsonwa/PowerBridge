# A short-term pullback strategy written by hand in the mirror dialect.
# el_emit turns it into EasyLanguage:  el_emit examples/hand_written.py
from pl_transpiler.runtime.pl_runtime import *


def strategy(open, high, low, close, volume, date, time, **kwargs):
    # Inputs: one kwargs.get per input, key equal to the name.
    trendlen = kwargs.get('trendlen', 20)
    dips = kwargs.get('dips', 2)
    stopusd = kwargs.get('stopusd', 300)
    # Variables: declared once, before any statement.
    trend = f32(0)
    exitlevel = f32(0)
    # Body.
    trend = f32(pl_average(close, trendlen))
    exitlevel = f32(high[-(1 + 1)])
    if _market_position == 0 and close[-1] > trend and close[-1] < close[-(dips + 1)]:
        _orders.append(('buy', 'market', 1, 'next', 'DipLE'))
    if _market_position == 1:
        _orders.append(('sell', 'limit', 0, 'next', 'TargetLX', exitlevel))
    _risk['stop_loss'] = stopusd
    return {'orders': _orders, 'plots': _plots, 'alerts': _alerts, 'risk': _risk}
