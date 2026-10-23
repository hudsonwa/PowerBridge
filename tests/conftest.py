"""Shared pytest hooks for the PowerBridge test suite."""

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


# tryfirst: mark before pytest applies -m (including the default in pytest.ini).
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
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


def pytest_terminal_summary(terminalreporter):
    """Group skipped tests by reason so a plain run shows why they skipped."""
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
