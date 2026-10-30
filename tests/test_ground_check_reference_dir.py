"""mc_ground_check --reference-dir / PB_MC_REFERENCE_DIR: optional reference files."""
import pytest

from pl_transpiler.tools import mc_ground_check as mgc

SRC = "Vars: x(0);\nx = Zzfakekeyword;\n"


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text(SRC)
    return str(p)


@pytest.fixture(autouse=True)
def _restore():
    old = (mgc.RESERVED, mgc.PDF)
    yield
    mgc.RESERVED, mgc.PDF = old


def test_unset_reference_dir_reports_not_proven(script, capsys):
    mgc.set_reference_dir(None)
    assert mgc.main([script]) == 1
    assert "zzfakekeyword" in capsys.readouterr().out


def test_reference_dir_reserved_words_prove_name(script, tmp_path, capsys):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "reserved_words.txt").write_text("<p>Zzfakekeyword</p>\n")
    assert mgc.main(["--reference-dir", str(ref), script]) == 0


def test_missing_files_skip_cross_checks(script, tmp_path):
    mgc.set_reference_dir(str(tmp_path / "nowhere"))
    assert mgc.check(script) == 1


def test_env_var_used_and_cli_wins(script, tmp_path, monkeypatch):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "reserved_words.txt").write_text("zzfakekeyword\n")
    monkeypatch.setenv(mgc.REFERENCE_ENV, str(ref))
    import importlib
    importlib.reload(mgc)
    assert mgc.check(script) == 0
    assert mgc.main(["--reference-dir", str(tmp_path / "empty"), script]) == 1
