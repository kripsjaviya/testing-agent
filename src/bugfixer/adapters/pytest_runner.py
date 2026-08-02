"""SuiteRunner adapter: runs the target's suite in a subprocess.

Structure comes from a plugin injected via ``-p`` and PYTHONPATH rather than from
parsing pytest's human-readable output: regexes over terminal text break on any
target that customises reporting, and text gives us no traceback frame chain.
Injection also means the target gains no dependency and none of its files change.

The target's own pytest configuration is left alone. A target configuring ``-x``
will surface one failure per run rather than all of them, which costs the agent
extra iterations but not correctness -- the loop re-runs until green regardless.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from ..domain.models import Frame, SuiteResult, CaseReport
from ..ports import SuiteRunError

log = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "_plugin"
_PLUGIN_MODULE = "_bugfixer_pytest_report"

DEFAULT_TIMEOUT_SECONDS = 600


class PytestRunner:
    """Real pytest execution. Satisfies the SuiteRunner port."""

    def run(
        self,
        root: Path,
        python: str,
        node_ids: list[str] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> SuiteResult:
        command = [
            python,
            "-m",
            "pytest",
            "-p",
            _PLUGIN_MODULE,
            "-p",
            "no:cacheprovider",
            "--tb=long",
            "-q",
        ]
        command += node_ids or []

        with tempfile.TemporaryDirectory(prefix="bugfixer-report-") as tmp:
            report_path = Path(tmp) / "report.json"
            env = _child_env(report_path)

            log.debug("running: %s (cwd=%s)", " ".join(command), root)
            started = time.monotonic()
            timed_out = False
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(root),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                stdout = completed.stdout
                stderr = completed.stderr
                exitstatus = completed.returncode
            except subprocess.TimeoutExpired as expired:
                timed_out = True
                stdout = _as_text(expired.stdout)
                stderr = _as_text(expired.stderr)
                exitstatus = -1
                log.warning("pytest exceeded %ss in %s", timeout, root)
            duration = time.monotonic() - started

            payload = _load_report(report_path)

        if payload is None:
            if timed_out:
                # Killed mid-session, so the plugin never wrote a report. Still a
                # usable signal: the caller needs to know the suite hangs.
                return SuiteResult(
                    exitstatus=exitstatus,
                    tests=(),
                    collection_errors=(),
                    duration=duration,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                )
            raise SuiteRunError(
                f"pytest produced no report (exit {exitstatus}). "
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )

        return SuiteResult(
            exitstatus=payload.get("exitstatus", exitstatus),
            tests=tuple(_to_report(t) for t in payload.get("tests", [])),
            collection_errors=tuple(payload.get("collection_errors", [])),
            duration=duration,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )


def _child_env(report_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_PLUGIN_DIR), existing]))
    env["BUGFIXER_REPORT_PATH"] = str(report_path)
    return env


def _load_report(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _to_report(raw: dict) -> CaseReport:
    return CaseReport(
        nodeid=raw["nodeid"],
        outcome=raw.get("outcome", "passed"),
        phase=raw.get("phase"),
        duration=raw.get("duration", 0.0),
        message=raw.get("message", ""),
        longrepr=raw.get("longrepr", ""),
        frames=tuple(
            Frame(path=f.get("path", ""), lineno=f.get("lineno"), message=f.get("message", ""))
            for f in raw.get("frames", [])
        ),
        sections=raw.get("sections", {}),
    )


def _as_text(stream) -> str:
    if stream is None:
        return ""
    return stream if isinstance(stream, str) else stream.decode("utf-8", "replace")
