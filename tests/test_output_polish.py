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
ACCEL_URL = "https://github.com/hudsonwa/PowerBridge/blob/main/docs/ACCEL.md#install"


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


def test_run_el_logs_python_path_note_instead_of_printing(monkeypatch, capsys, caplog):
    import logging
    from pl_transpiler.runtime.instrument_config import get_config
    monkeypatch.setattr("pl_transpiler.runtime.accel._ACCEL_MISSING_WARNED", False)
    with open(EL, encoding="utf-8") as f:
        source = f.read()
    with caplog.at_level(logging.INFO, logger="pl_transpiler.tools.pl_run"):
        pl_run.run_el(source, pl_run.load_bars_csv(BARS), get_config("NQ"))
    assert "pure-Python engine" not in capsys.readouterr().err
    assert any("pure-Python engine" in r.getMessage() for r in caplog.records)


def test_pl_run_cli_prints_not_installed_note_only_with_verbose(monkeypatch, capsys,
                                                               tmp_path):
    monkeypatch.setattr("pl_transpiler.runtime.accel.accel_available", lambda: False)
    monkeypatch.setattr("pl_transpiler.runtime.accel._ACCEL_MISSING_WARNED", False)
    assert pl_run.main([EL, BARS, "-o", str(tmp_path / "out.csv")]) == 0
    assert "not installed" not in capsys.readouterr().err
    assert pl_run.main([EL, BARS, "-v", "-o", str(tmp_path / "out.csv")]) == 0
    err = capsys.readouterr().err
    assert "note: powerbridge-accel not installed" in err
    assert ACCEL_URL in err


def test_pl_oracle_prints_not_installed_note_only_with_verbose(monkeypatch, capsys):
    monkeypatch.setattr(pl_oracle, "accel_available", lambda: False)
    monkeypatch.setattr("pl_transpiler.runtime.accel._ACCEL_MISSING_WARNED", False)
    assert pl_oracle.main([EL, BARS]) == 0
    assert "not installed" not in capsys.readouterr().err
    assert pl_oracle.main([EL, BARS, "--verbose"]) == 0
    err = capsys.readouterr().err
    assert "note: powerbridge-accel not installed" in err
    assert ACCEL_URL in err


def test_not_installed_note_url_anchor_exists():
    # the note is read in a terminal, so it names a full URL; its anchor must exist
    accel_md = os.path.join(REPO, "docs", "ACCEL.md")
    assert ACCEL_URL.endswith("/docs/ACCEL.md#install")
    assert "\n## Install\n" in open(accel_md, encoding="utf-8").read()


def test_pl_run_cli_prints_other_fallback_notes_without_verbose(monkeypatch, capsys,
                                                                tmp_path):
    monkeypatch.setattr("pl_transpiler.runtime.accel.accel_available", lambda: True)
    monkeypatch.setattr("pl_transpiler.runtime.accel._ACCEL_MISSING_WARNED", False)
    assert pl_run.main([EL, BARS, "-o", str(tmp_path / "out.csv")]) == 0
    assert "note: using the pure-Python engine" in capsys.readouterr().err
