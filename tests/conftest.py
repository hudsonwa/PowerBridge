"""Shared pytest hooks for the PowerBridge test suite."""

import importlib.util
from collections import Counter

import pytest

# Marker groups (registered in pytest.ini). A test whose skipif reason names
# one of these is marked automatically; runtime skips carry the mark by hand.
_REASON_MARKERS = (
    ("needs_native", ("native kernel", "accel extension", "pl_transpiler._accel")),
    ("needs_captures", ("captures", "drawing corpus", "audit tape")),
    ("perf", ("PB_PERF",)),
)

# Runtime skips in stdlib-only test scripts, which cannot import pytest marks.
_STDLIB_MARKS = {
    "test_oracle.py::test_reference_audit_python_data2_bars_matches_closes": "needs_captures",
    "test_audit_packet.py::test_tiny_el_packet_matches_run_native_fills": "needs_native",
    "test_audit_packet.py::test_packet_does_not_require_dropped_slots": "needs_native",
    "test_fill_log.py::test_native_fills_match_python": "needs_native",
}

_NATIVE_MISSING = ("not built", "not installed", "No module named")

_NATIVE_NOTE = "native kernel not built: {n} native-parity tests not collected " \
    "(build with tools/build_accel.sh, see docs/ACCEL.md)"


def _kernel_built():
    try:
        return importlib.util.find_spec("pl_transpiler._accel") is not None
    except ImportError:
        return False

# Skip categories for the end-of-run summary: (label, needles). The first whose
# needle appears in the reason wins; anything else is listed under "other".
_SKIP_CATEGORIES = (
    ("native extension not built (expected on a fresh clone): "
     "pip install -e '.[dev]', then ./tools/build_accel.sh; see docs/ACCEL.md",
     ("native kernel", "accel extension", "pl_transpiler._accel")),
    ("licensed MultiCharts captures not shipped (expected on a fresh clone); "
     "see docs/CAPTURES.md",
     ("captures", "drawing corpus", "audit tape", "private fixture")),
    ("optional tool missing (expected without the dev extras): "
     "pip install -e '.[dev]'",
     ("'build' module", "pip install")),
    ("case does not apply (by design)",
     ("engine kwargs key", "no round trip", "original is not proven")),
)


def skip_category(reason, native=False):
    """Summary label for a skip `reason`; None means uncategorised ("other")."""
    if native and any(n in reason for n in _NATIVE_MISSING):
        return _SKIP_CATEGORIES[0][0]
    for label, needles in _SKIP_CATEGORIES:
        if any(n in reason for n in needles):
            return label
    return None


# A wrapper: mark before pytest applies -m (including the default in
# pytest.ini), then leave out the native tests from what -m kept.
@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config, items):
    native_only = {id(i) for i in items if _native_only(i)}
    for item in items:
        reasons = " ".join(str(m.kwargs.get("reason", ""))
                           for m in item.iter_markers("skipif"))
        for marker, needles in _REASON_MARKERS:
            if any(n in reasons for n in needles):
                item.add_marker(marker)
        marker = _STDLIB_MARKS.get(item.nodeid.rpartition("/")[2])
        if marker:
            item.add_marker(marker)
        callspec = getattr(item, "callspec", None)
        if callspec is not None and callspec.params.get("cap_dep"):
            item.add_marker(pytest.mark.needs_captures)
    yield
    native_off = [i for i in items if id(i) in native_only]
    # Without the kernel every needs_native test would skip; leave them out of
    # the run (one summary line says so) unless -m asks for them by name.
    if (native_off and "needs_native" not in (config.option.markexpr or "")
            and not _kernel_built()):
        items[:] = [i for i in items if id(i) not in native_only]
        config.hook.pytest_deselected(items=native_off)
        config._pb_native_off = len(native_off)


def _native_only(item):
    """True if `item` skips whenever the kernel is missing."""
    # Called before the reason markers are added, so only a hand-written mark
    # counts (a skipif that names the kernel is checked for its condition below).
    if any(item.iter_markers("needs_native")) \
            or _STDLIB_MARKS.get(item.nodeid.rpartition("/")[2]) == "needs_native":
        return True
    needles = _REASON_MARKERS[0][1]
    for m in item.iter_markers("skipif"):
        reason = str(m.kwargs.get("reason", ""))
        # Plain bool conditions only; a string condition is left to pytest.
        if any(n in reason for n in needles) and m.args \
                and all(isinstance(a, bool) and a for a in m.args):
            return True
    return False


def pytest_terminal_summary(terminalreporter):
    """Group skipped tests by reason so a plain run shows why they skipped."""
    native_off = getattr(terminalreporter.config, "_pb_native_off", 0)
    if native_off:
        terminalreporter.write_sep("-", _NATIVE_NOTE.format(n=native_off))
    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    groups = Counter()
    other = Counter()
    for report in skipped:
        longrepr = report.longrepr
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            reason = str(longrepr[2])
        else:
            reason = str(longrepr)
        if reason.startswith("Skipped: "):
            reason = reason[len("Skipped: "):]
        reason = reason.strip()
        label = skip_category(reason, "needs_native" in report.keywords)
        if label is None:
            other[reason] += 1
        else:
            groups[label] += 1
    terminalreporter.write_sep("-", f"skip summary ({len(skipped)} skipped)")
    for label, count in groups.most_common():
        terminalreporter.write_line(f"{count:5d}  {label}")
    if other:
        terminalreporter.write_line(f"{sum(other.values()):5d}  other:")
        for reason, count in other.most_common():
            terminalreporter.write_line(f"{count:9d}  {reason}")
    terminalreporter.write_line(
        "Not run by default: -m needs_captures (licensed MultiCharts data), "
        "-m perf (PB_PERF=1). See CONTRIBUTING.md (Running checks locally)."
    )
