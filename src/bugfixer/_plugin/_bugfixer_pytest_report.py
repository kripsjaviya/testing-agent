"""Pytest plugin injected into the target's test run to emit a structured report.

Loaded with ``-p _bugfixer_pytest_report`` and a PYTHONPATH entry pointing at this
directory, so the target repo gains no dependency and none of its files are touched.
Writes JSON to the path in ``BUGFIXER_REPORT_PATH``.

This lives outside the ``bugfixer`` package on purpose: it is imported by the
*target's* interpreter, which knows nothing about the agent.
"""

import json
import os

_REPORT_ENV = "BUGFIXER_REPORT_PATH"


class _Collector:
    def __init__(self):
        self.tests = {}
        self.collection_errors = []

    def record(self, report):
        entry = self.tests.setdefault(
            report.nodeid,
            {"nodeid": report.nodeid, "outcome": "passed", "phase": None, "duration": 0.0},
        )
        entry["duration"] += getattr(report, "duration", 0.0) or 0.0

        # A test is only as good as its worst phase: a passing call followed by a
        # teardown error is still a failure, and teardown is exactly where resource
        # leaks surface.
        if report.outcome == "passed":
            return
        if entry["outcome"] == "failed" and report.outcome == "skipped":
            return

        entry["outcome"] = report.outcome
        entry["phase"] = report.when
        entry.update(_describe(report.longrepr))
        entry["sections"] = {name: content for name, content in getattr(report, "sections", [])}


def _describe(longrepr):
    """Flatten pytest's several longrepr shapes into plain JSON-safe fields."""
    if longrepr is None:
        return {"message": "", "longrepr": "", "frames": []}

    # Skips arrive as (path, lineno, reason).
    if isinstance(longrepr, tuple):
        path, lineno, reason = longrepr
        return {
            "message": str(reason),
            "longrepr": str(reason),
            "frames": [{"path": str(path), "lineno": lineno, "message": str(reason)}],
        }

    crash = getattr(longrepr, "reprcrash", None)
    message = str(getattr(crash, "message", "")) if crash else ""

    frames = []
    traceback = getattr(longrepr, "reprtraceback", None)
    for entry in getattr(traceback, "reprentries", []) or []:
        loc = getattr(entry, "reprfileloc", None)
        if loc is None:
            continue
        frames.append(
            {
                "path": str(loc.path),
                "lineno": loc.lineno,
                "message": str(loc.message),
            }
        )

    return {"message": message, "longrepr": str(longrepr), "frames": frames}


_collector = _Collector()


def pytest_runtest_logreport(report):
    _collector.record(report)


def pytest_collectreport(report):
    if report.outcome == "failed":
        _collector.collection_errors.append(
            {"nodeid": report.nodeid, **_describe(report.longrepr)}
        )


def pytest_sessionfinish(session, exitstatus):
    destination = os.environ.get(_REPORT_ENV)
    if not destination:
        return

    tests = list(_collector.tests.values())
    summary = {}
    for test in tests:
        summary[test["outcome"]] = summary.get(test["outcome"], 0) + 1

    payload = {
        "exitstatus": int(exitstatus),
        "summary": summary,
        "tests": tests,
        "collection_errors": _collector.collection_errors,
    }
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
