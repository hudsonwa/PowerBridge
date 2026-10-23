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

_GROUP_REASONS = {
    "needs_native": "needs_native: native kernel not built; run ./tools/build_accel.sh",
}


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
    reasons = Counter()
    for report in skipped:
        longrepr = report.longrepr
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            reason = str(longrepr[2])
        else:
            reason = str(longrepr)
        if reason.startswith("Skipped: "):
            reason = reason[len("Skipped: "):]
        reason = reason.strip()
        # Collapse the many spellings of "kernel missing" into one line; other
        # needs_native skips (a program the kernel refuses) keep their reason.
        if "needs_native" in report.keywords and any(
                n in reason for n in _NATIVE_MISSING):
            reason = _GROUP_REASONS["needs_native"]
        reasons[reason] += 1
    terminalreporter.write_sep("-", f"skip summary ({len(skipped)} skipped)")
    for reason, count in reasons.most_common():
        terminalreporter.write_line(f"{count:5d}  {reason}")
    terminalreporter.write_line(
        "Not run by default: -m needs_captures (licensed MultiCharts data), "
        "-m perf (PB_PERF=1). See CONTRIBUTING.md (Running checks locally)."
    )
