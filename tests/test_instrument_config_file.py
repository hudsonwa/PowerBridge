"""--config accepts a JSON file with exactly the InstrumentConfig fields."""
import json
import os

import pytest

from pl_transpiler.runtime import accel
from pl_transpiler.runtime.instrument_config import (
    ES,
    NQ,
    ConfigFileError,
    get_config,
    load_config_file,
)
from tests.test_cli_errors import BARS, EL, _assert_one_line, _run

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS = os.path.join(REPO, "examples", "configs")


@pytest.mark.parametrize("builtin", [ES, NQ])
def test_example_json_equals_builtin(builtin):
    path = os.path.join(CONFIGS, builtin.name + ".json")
    loaded = load_config_file(path)
    assert loaded == builtin
    assert get_config(path) == builtin
    # The native kernel receives the same FFI config mapping.
    assert accel._config_dict(loaded) == accel._config_dict(builtin)


def _write(tmp_path, data):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(data))
    return str(p)


def _es():
    return json.load(open(os.path.join(CONFIGS, "ES.json")))


def test_category_optional(tmp_path):
    d = _es()
    del d["category"]
    assert load_config_file(_write(tmp_path, d)) == ES


def test_round_turn_cost_optional_and_sent_to_kernel(tmp_path):
    d = _es()
    del d["round_turn_cost"]
    assert load_config_file(_write(tmp_path, d)) == ES
    d["round_turn_cost"] = 4.5
    loaded = load_config_file(_write(tmp_path, d))
    assert loaded.round_turn_cost == 4.5
    assert accel._config_dict(loaded)["round_turn_cost"] == 4.5
    d["round_turn_cost"] = 0
    assert load_config_file(_write(tmp_path, d)).round_turn_cost == 0


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(commission=2.5), "unknown key(s) commission"),
    (lambda d: d.pop("tick"), "missing key(s) tick"),
    (lambda d: d.update(minmove="25"), "minmove must be int"),
    (lambda d: d.update(minmove=25.0), "minmove must be int"),
    (lambda d: d.update(tick=True), "tick must be float"),
    (lambda d: d.update(symnm=1), "symnm must be str"),
    (lambda d: d.update(round_turn_cost="4.5"), "round_turn_cost must be float"),
    (lambda d: d.update(round_turn_cost=-1.0), "round_turn_cost must be a finite number >= 0"),
    (lambda d: d.update(round_turn_cost=float("inf")), "round_turn_cost must be a finite number >= 0"),
])
def test_bad_config_file(tmp_path, mutate, needle):
    d = _es()
    mutate(d)
    with pytest.raises(ConfigFileError, match=needle.replace("(", r"\(").replace(")", r"\)")):
        load_config_file(_write(tmp_path, d))


def test_not_an_object_and_bad_json(tmp_path):
    with pytest.raises(ConfigFileError, match="JSON object"):
        load_config_file(_write(tmp_path, [1]))
    p = tmp_path / "broken.json"
    p.write_text("{")
    with pytest.raises(ConfigFileError, match="invalid JSON"):
        load_config_file(str(p))


@pytest.mark.parametrize("tool", ["pl_run", "pl_oracle"])
def test_cli_bad_config_file_one_line(tool, tmp_path):
    d = _es()
    d["slippage"] = 1
    _assert_one_line(_run(tool, EL, BARS, "--config", _write(tmp_path, d)),
                     tool, "unknown key(s) slippage")
    _assert_one_line(_run(tool, EL, BARS, "--config", str(tmp_path / "nope.json")),
                     tool, "No such file")


@pytest.mark.parametrize("args", [[], ["--python"]])
def test_oracle_json_matches_builtin(args):
    by_name = _run("pl_oracle", EL, BARS, "--config", "ES", *args)
    by_file = _run("pl_oracle", EL, BARS, "--config",
                   os.path.join(CONFIGS, "ES.json"), *args)
    assert by_name.returncode == 0, by_name.stderr
    assert by_file.stdout == by_name.stdout
