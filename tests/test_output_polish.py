"""User-facing output rounding and the single native-refusal note."""

import csv
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pl_transpiler.runtime.accel import AccelRefused  # noqa: E402
from pl_transpiler.tools import pl_oracle, pl_run  # noqa: E402
from pl_transpiler.tools.pl_run import display_row_sink, tick_decimals  # noqa: E402

EL = os.path.join(REPO, "examples", "ma_cross.el")
BARS = os.path.join(REPO, "examples", "NQ_sample_bars.csv")


def test_tick_decimals():
    assert tick_decimals(0.25) == 2
    assert tick_decimals(1.0) == 0
    assert tick_decimals(0.0001) == 4


def test_display_row_sink_rounds_money_and_prices_only():
    out = []
    header, row = display_row_sink(out.append, 0.25)
    header(["NetProfit", "EntryPrice", "TotalTrades", "myvar"])
    row(["1343.7984375000087", "15045.7998046875", "1", "0.1234567"])
    row(["0", "", "0", "x"])
    assert out[1] == ["1343.80", "15045.80", "1", "0.1234567"]
    assert out[2] == ["0", "", "0", "x"]


def _run_csv(tmp_path, *extra):
    path = tmp_path / "out.csv"
    assert pl_run.main([EL, BARS, "--config", "NQ", "--columns",
                        "NetProfit,EntryPrice", "-o", str(path), *extra]) == 0
    return list(csv.reader(open(path, encoding="utf-8")))[1:]


def test_pl_run_csv_rounded_by_default(tmp_path):
    rows = _run_csv(tmp_path)
    assert ["1343.80", "14972.51"] in rows
    for net, price in rows:
        assert len(net.split(".")[1]) == 2 and len(price.split(".")[1]) == 2


def test_pl_run_full_precision_keeps_raw_values(tmp_path):
    rows = _run_csv(tmp_path, "--full-precision")
    assert any(net == "1343.7984375000087" for net, _ in rows)


def test_fills_csv_price_rounded_digest_unchanged(tmp_path):
    fills = tmp_path / "fills.csv"
    digest = tmp_path / "digest.txt"
    assert pl_oracle.main([EL, BARS, "--python", "--fills-csv", str(fills),
                           "-o", str(digest)]) == 0
    rows = list(csv.DictReader(open(fills, encoding="utf-8")))
    assert rows and all(len(r["price"].split(".")[1]) == 2 for r in rows)
    assert "f2ab719a0985319cad5109e9ba8879a0fd6eac4849725920ba817c13fe0474da" in \
        digest.read_text()


def test_native_refusal_reported_once(monkeypatch, capsys):
    def refuse(*_a, **_k):
        raise AccelRefused("unsupported construct")

    monkeypatch.setattr(pl_oracle, "accel_available", lambda: True)
    monkeypatch.setattr(pl_oracle, "run_native", refuse)
    monkeypatch.setattr("pl_transpiler.runtime.accel._ACCEL_MISSING_WARNED", False)
    assert pl_oracle.main([EL, BARS]) == 0
    err = capsys.readouterr().err
    assert err.count("native path refused") == 1, err
    assert "using the pure-Python engine" in err
