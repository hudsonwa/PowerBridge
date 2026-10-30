"""tools/skip_report.py: --check passes on a matching table and fails on drift."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "skip_report", os.path.join(ROOT, "tools", "skip_report.py"))
skip_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skip_report)

ROWS = [("deselected", "needs_captures"), ("deselected", "needs_captures"),
        ("skipped", "kernel refuses this program: while loops"),
        ("skipped", "native kernel not built: build it")]


def _doc(tmp_path, monkeypatch, text):
    target = tmp_path / "CONTRIBUTING.md"
    target.write_text("intro\n\n%s\n\n%s\n\n%s\n" % (skip_report.BEGIN, text,
                                                   skip_report.END), encoding="utf-8")
    monkeypatch.setattr(skip_report, "TARGET", str(target))


def test_table_groups_runtime_native_skips_under_the_marker():
    text = skip_report.table(ROWS)
    assert "| `needs_captures` | 2 | deselected |" in text
    assert "| kernel refuses this program | 1 | skipped |" in text
    assert "| `needs_native` | 1 | skipped |" in text


def test_check_ignores_generated_line(tmp_path, monkeypatch, capsys):
    text = skip_report.table(ROWS)
    _doc(tmp_path, monkeypatch, text.replace("Generated with", "Made with"))
    assert skip_report.check(text, skip_report.BEGIN, skip_report.END) == 0


def test_check_fails_on_drift(tmp_path, monkeypatch, capsys):
    _doc(tmp_path, monkeypatch, skip_report.table(ROWS[1:]))
    assert skip_report.check(skip_report.table(ROWS), skip_report.BEGIN,
                             skip_report.END) == 1
    err = capsys.readouterr().err
    assert "out of date" in err
    assert "+| `needs_captures` | 2 | deselected |" in err
