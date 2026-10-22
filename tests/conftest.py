"""Shared pytest hooks for the PowerBridge test suite."""

from collections import Counter


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
        reasons[reason.strip()] += 1
    terminalreporter.write_sep("-", f"skip summary ({len(skipped)} skipped)")
    for reason, count in reasons.most_common():
        terminalreporter.write_line(f"{count:5d}  {reason}")
    terminalreporter.write_line(
        "See CONTRIBUTING.md (Running checks locally) for what each group needs."
    )
