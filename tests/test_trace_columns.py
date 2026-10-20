"""--columns / trace_columns: EL aliases resolve, unknown names fail loud (#53)."""
import contextlib
import io
import os
import tempfile

import pytest

from pl_transpiler.runtime.instrument_config import get_config
from pl_transpiler.tools.pl_run import (
    UnknownTraceColumnError, load_bars_csv, main, resolve_trace_columns, run_el)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EL = os.path.join(ROOT, "examples", "ma_cross.el")
BARS = os.path.join(ROOT, "examples", "NQ_sample_bars.csv")


def _run(cols):
    return run_el(open(EL, encoding="utf-8").read(), load_bars_csv(BARS),
                  get_config("NQ"), trace_columns=cols)


def test_resolve_aliases():
    keys = ["_market_position", "net_profit", "_barnumber", "totaltrades",
            "open_position_profit"]
    assert resolve_trace_columns(
        ["MarketPosition", "NetProfit", "marketpos", "CurrentBar",
         "TOTALTRADES", "OpenPositionProfit", "net_profit"], keys) == [
        "_market_position", "net_profit", "_market_position", "_barnumber",
        "totaltrades", "open_position_profit", "net_profit"]


def test_aliases_match_real_keys_and_keep_header():
    alias = _run(["MarketPosition", "NetProfit"])
    real = _run(["_market_position", "net_profit"])
    assert alias["columns"] == ["MarketPosition", "NetProfit"]
    assert alias["rows"] == real["rows"]
    assert any(r[0] != "0" for r in alias["rows"])


def test_unknown_column_raises_with_suggestion():
    with pytest.raises(UnknownTraceColumnError) as ei:
        _run(["MarketPositon", "bogus"])
    msg = str(ei.value)
    assert isinstance(ei.value, ValueError)
    assert "'MarketPositon' (did you mean MarketPosition?)" in msg
    assert "_market_position" not in msg  # EL names, not internal trace keys
    assert "'bogus'" in msg and "\n" not in msg


def test_cli_unknown_column_exits_2():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.csv")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main([EL, BARS, "--columns", "marketpos,nope", "-o", out])
        assert rc == 2
        line = err.getvalue().strip().splitlines()[-1]
        assert line.startswith("pl_run: error: unknown trace column(s): 'nope'")
        assert open(out, encoding="utf-8").read() == ""
