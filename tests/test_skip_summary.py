"""The end-of-run skip summary puts each known skip reason in its category."""

from tests.conftest import _SKIP_CATEGORIES, skip_category
from tests.skip_messages import ACCEL_SKIP, AUDIT_TAPE_SKIP

NATIVE, CAPTURES, TOOL, BY_DESIGN = (label for label, _ in _SKIP_CATEGORIES)


def test_known_reasons_map_to_categories():
    assert skip_category(ACCEL_SKIP) == NATIVE
    assert skip_category("native kernel not built") == NATIVE
    assert skip_category("could not import 'pl_transpiler._accel': "
                         "No module named 'pl_transpiler._accel'") == NATIVE
    assert skip_category(AUDIT_TAPE_SKIP) == CAPTURES
    assert skip_category("needs MultiCharts captures (not redistributed)") == CAPTURES
    assert skip_category("needs the 'build' module and setuptools>=77 to build "
                         "the wheel offline: pip install -e '.[dev]'") == TOOL
    assert skip_category("input name is an engine kwargs key") == BY_DESIGN


def test_needs_native_mark_groups_missing_kernel_spellings():
    assert skip_category("No module named 'x'", native=True) == NATIVE
    assert skip_category("No module named 'x'") is None


def test_unknown_reason_is_other():
    assert skip_category("something new") is None


def test_setup_hints_name_the_dev_install():
    assert "pip install -e '.[dev]'" in ACCEL_SKIP
    assert "docs/ACCEL.md" in ACCEL_SKIP
