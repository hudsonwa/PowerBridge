"""Default ``pl_transpile`` output is runnable (issue #68).

Each examples/*.el is transpiled with default CLI options in a subprocess, then
the emitted file is imported and driven bar by bar over the sample bars the way
the README shows. It must not raise and must emit order signals.
"""
import csv
import glob
import importlib.util
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BARS = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
EXAMPLES = sorted(glob.glob(os.path.join(REPO, "examples", "*.el")))


def _transpile(el, out, *flags):
    proc = subprocess.run(
        [sys.executable, "-m", "pl_transpiler.tools.pl_transpile", el, "-o", str(out), *flags],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return out.read_text()


def _load_strategy(path):
    spec = importlib.util.spec_from_file_location("pb_emitted_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.strategy


def _run(strategy):
    cols = {k: [] for k in ("open", "high", "low", "close", "volume", "date", "time")}
    kwargs = {}
    orders = []
    with open(BARS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for k in ("open", "high", "low", "close", "volume"):
                cols[k].append(float(row[k]))
            cols["date"].append(int(row["date"]))
            cols["time"].append(int(row["time"]))
            orders.extend(strategy(*cols.values(), kwargs)["orders"])
    return orders


def test_examples_present():
    assert EXAMPLES


@pytest.mark.parametrize("el", EXAMPLES, ids=os.path.basename)
def test_default_output_runs(el, tmp_path):
    src = _transpile(el, tmp_path / "default.py")
    assert src == _transpile(el, tmp_path / "persist.py", "--persist")
    orders = _run(_load_strategy(tmp_path / "default.py"))
    assert orders, "default output ran but emitted no orders"


def test_no_persist_is_mirror_dialect(tmp_path):
    el = os.path.join(REPO, "examples", "ma_cross.el")
    default = _transpile(el, tmp_path / "default.py")
    mirror = _transpile(el, tmp_path / "mirror.py", "--no-persist")
    assert "kwargs.setdefault('_state'" in default
    assert "kwargs.setdefault('_state'" not in mirror
