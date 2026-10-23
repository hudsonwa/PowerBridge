"""User mistakes in the CLIs exit 2 with one ``<tool>: error: ...`` line, no traceback.

Each case runs the real entry point in a subprocess; ``PB_DEBUG=1`` restores
the traceback.
"""
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EL = os.path.join(REPO, "examples", "ma_cross.el")
BARS = os.path.join(REPO, "examples", "NQ_sample_bars.csv")


def _run(tool, *args, env_extra=None):
    env = dict(os.environ, PB_NO_PBAR="1")
    env.pop("PB_DEBUG", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "pl_transpiler.tools." + tool, *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)


def _assert_one_line(proc, tool, needle):
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith(tool + ": error: "), lines[0]
    assert needle in lines[0], lines[0]


@pytest.fixture
def files(tmp_path):
    bad_el = tmp_path / "bad.el"
    bad_el.write_text("if close > then buy next bar at market;\n")
    no_hdr = tmp_path / "nohdr.csv"
    no_hdr.write_text("date,time,open,high,low,close\n1240102,930,1,2,0,1\n")
    bad_row = tmp_path / "badrow.csv"
    bad_row.write_text("date,time,open,high,low,close,volume\n1240102,930,x,2,0,1,5\n")
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    header_only = tmp_path / "header_only.csv"
    header_only.write_text("date,time,open,high,low,close,volume\n")
    return {"bad_el": str(bad_el), "no_hdr": str(no_hdr),
            "bad_row": str(bad_row), "empty": str(empty),
            "header_only": str(header_only),
            "missing": str(tmp_path / "nope.el")}


@pytest.mark.parametrize("tool", ["pl_transpile", "el_emit"])
def test_missing_input(tool, files):
    _assert_one_line(_run(tool, files["missing"]), tool, "No such file")


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
def test_missing_el_and_bars(tool, files):
    _assert_one_line(_run(tool, files["missing"], BARS), tool, "No such file")
    _assert_one_line(_run(tool, EL, files["missing"]), tool, "No such file")


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
def test_unknown_config_lists_known(tool):
    _assert_one_line(_run(tool, EL, BARS, "--config", "ZZ"), tool, "'NQ'")


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
@pytest.mark.parametrize("which,needle", [
    ("no_hdr", "missing column(s) volume"),
    ("bad_row", "line 2"),
    ("empty", "empty file"),
    ("header_only", "contains no bars"),
])
def test_bad_csv(tool, files, which, needle):
    _assert_one_line(_run(tool, EL, files[which]), tool, needle)


@pytest.mark.parametrize("tool,args", [
    ("pl_transpile", ()), ("el_emit", ()), ("pl_run", (BARS,)), ("pl_oracle", (BARS,)),
])
def test_syntax_error_keeps_caret_block(tool, args, files):
    proc = _run(tool, files["bad_el"], *args)
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    lines = proc.stderr.splitlines()
    assert lines[0].startswith(tool + ": error: EasyLanguage syntax error at line 1:"), lines
    assert any(ln.strip() == "^" for ln in lines), proc.stderr


def test_pb_debug_reraises(files):
    proc = _run("pl_transpile", files["missing"], env_extra={"PB_DEBUG": "1"})
    assert proc.returncode != 0
    assert "Traceback" in proc.stderr


def test_dash_output_is_stdout():
    plain = _run("pl_transpile", EL)
    dash = _run("pl_transpile", EL, "-o", "-")
    assert dash.returncode == plain.returncode == 0
    assert dash.stdout == plain.stdout
    assert not os.path.exists(os.path.join(REPO, "-"))


def test_pl_run_summary_wording():
    proc = _run("pl_run", EL, BARS, "-o", "-")
    assert proc.returncode == 0, proc.stderr
    summary = proc.stderr.strip().splitlines()[-1]
    assert " fills, " in summary and " round-turns" in summary, summary


def test_pl_oracle_status_line_has_no_jargon():
    proc = _run("pl_oracle", EL, BARS, "--python")
    assert proc.returncode == 0, proc.stderr
    summary = [ln for ln in proc.stderr.splitlines() if ln.startswith("[pl_oracle]")]
    assert summary == ["[pl_oracle] 140 bars, 3 fills, 1 round-turns [python]"], proc.stderr


def test_pl_oracle_verbose_explains_skipped_bars():
    proc = _run("pl_oracle", EL, BARS, "--python", "--verbose")
    assert proc.returncode == 0, proc.stderr
    assert any(ln.startswith("[pl_oracle] ") and "skipped fill processing" in ln
               for ln in proc.stderr.splitlines()), proc.stderr


@pytest.mark.needs_native
def test_pl_oracle_accel_internals_need_vv():
    pytest.importorskip("pl_transpiler._accel", reason="native kernel not built")
    for flag, shown in (("-v", False), ("-vv", True)):
        proc = _run("pl_oracle", EL, BARS, "--native", flag, env_extra={"PB_ACCEL_VERBOSE": ""})
        assert proc.returncode == 0, proc.stderr
        assert any(ln.startswith("[accel] ") for ln in proc.stderr.splitlines()) is shown, \
            (flag, proc.stderr)


UNIMPL_EL = "Vars: X(0);\nX = TradesToday(0);\nIf X > 5 then Buy next bar at market;\n"
UNDECLARED_EL = "Vars: X(0);\nMyTotal = 3;\n"


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
def test_unimplemented_keyword_one_line(tool, tmp_path):
    el = tmp_path / "u.el"
    el.write_text(UNIMPL_EL)
    _assert_one_line(_run(tool, str(el), BARS), tool,
                     "unimplemented EL keyword 'tradestoday' at line 2")


def test_el_emit_unimplemented_keyword_no_traceback(tmp_path):
    el = tmp_path / "u.el"
    el.write_text(UNIMPL_EL)
    proc = _run("el_emit", str(el))
    assert "Traceback" not in proc.stderr
    assert "tradestoday" in proc.stdout + proc.stderr


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
def test_undeclared_variable_one_line(tool, tmp_path):
    el = tmp_path / "v.el"
    el.write_text(UNDECLARED_EL)
    _assert_one_line(_run(tool, str(el), BARS), tool,
                     "undeclared variable 'MyTotal' at line 2 (declare it under Vars:)")


def test_pl_transpile_undeclared_variable(tmp_path):
    el = tmp_path / "v.el"
    el.write_text(UNDECLARED_EL)
    proc = _run("pl_transpile", str(el))
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "undeclared variable 'MyTotal' at line 2" in proc.stderr
    assert "unimplemented EL keyword 'mytotal'" not in proc.stderr


@pytest.mark.parametrize("src,needle", [
    ("A = sum(close[-20:])\n", "at line 1"),
    ("def strategy(:\n    pass\n", "not valid Python"),
])
def test_el_emit_off_dialect_python(tmp_path, src, needle):
    py = tmp_path / "bad.py"
    py.write_text(src)
    _assert_one_line(_run("el_emit", str(py)), "el_emit", needle)


@pytest.mark.parametrize("tool", ["pl_transpile", "pl_run", "pl_oracle", "el_emit"])
def test_version(tool):
    from pl_transpiler import __version__
    proc = _run(tool, "--version")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "%s %s" % (tool, __version__)
