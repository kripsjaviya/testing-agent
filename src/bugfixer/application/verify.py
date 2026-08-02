"""Deciding whether a patch actually worked.

Two failure modes this exists to separate. A test that fails intermittently will
pass once by luck, so a single green run is not proof of a fix -- the targeted
test is re-run until it has passed consistently. And a patch that fixes its own
test while breaking another is not a fix, so the full suite runs afterwards and is
compared against the failures recorded *before* the patch: only tests that were
passing and now fail count as regressions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..domain.models import SuiteResult
from ..domain.trace import Kind, Tracer
from ..ports import SuiteRunner

log = logging.getLogger(__name__)

DEFAULT_REPEATS = 3


@dataclass(frozen=True)
class Verification:
    nodeid: str
    targeted_outcomes: tuple[bool, ...]  # one per repeat, True = passed
    regressions: tuple[str, ...] = ()
    suite: SuiteResult | None = None
    detail: str = ""

    @property
    def targeted_passed(self) -> bool:
        return bool(self.targeted_outcomes) and all(self.targeted_outcomes)

    @property
    def flaky(self) -> bool:
        """Passed sometimes and failed sometimes -- neither fixed nor clearly broken."""
        return len(set(self.targeted_outcomes)) > 1

    @property
    def regressed(self) -> bool:
        return bool(self.regressions)

    @property
    def fixed(self) -> bool:
        return self.targeted_passed and not self.regressed

    def summary(self) -> str:
        passed = sum(self.targeted_outcomes)
        total = len(self.targeted_outcomes)
        if self.flaky:
            return f"flaky: {passed}/{total} targeted runs passed"
        if not self.targeted_passed:
            return f"still failing: 0/{total} targeted runs passed"
        if self.regressed:
            return f"regressed {len(self.regressions)} other test(s)"
        return f"fixed: {passed}/{total} targeted runs passed, no regressions"


class Verifier:
    """Runs the targeted test repeatedly, then the full suite."""

    def __init__(
        self,
        runner: SuiteRunner,
        repeats: int = DEFAULT_REPEATS,
        tracer: Tracer | None = None,
        check_regressions: bool = True,
    ):
        if repeats < 1:
            raise ValueError("repeats must be at least 1")
        self._runner = runner
        self._repeats = repeats
        self._tracer = tracer
        self._check_regressions = check_regressions

    def __call__(
        self,
        repo_root,
        python: str,
        nodeid: str,
        baseline_failures: set[str],
        timeout: int = 600,
    ) -> Verification:
        outcomes: list[bool] = []
        for attempt in range(self._repeats):
            result = self._runner.run(repo_root, python, node_ids=[nodeid], timeout=timeout)
            passed = not result.timed_out and nodeid not in set(result.failing_ids)
            outcomes.append(passed)

            # A test that has already failed will not become fixed by running it
            # again; stop early rather than burning two more runs.
            if not passed:
                break

        verification = Verification(nodeid=nodeid, targeted_outcomes=tuple(outcomes))
        if not verification.targeted_passed or not self._check_regressions:
            # Deferred mode trades per-patch attribution for speed on slow suites:
            # the run's closing full-suite pass is then the only regression signal,
            # and it cannot say which patch caused one.
            self._trace(verification)
            return verification

        suite = self._runner.run(repo_root, python, timeout=timeout)
        # Only tests that were passing before and fail now are regressions; a test
        # that was already red is someone else's bug, still queued.
        regressions = tuple(sorted(set(suite.failing_ids) - baseline_failures - {nodeid}))

        verification = Verification(
            nodeid=nodeid,
            targeted_outcomes=tuple(outcomes),
            regressions=regressions,
            suite=suite,
        )
        self._trace(verification)
        return verification

    def _trace(self, verification: Verification) -> None:
        if self._tracer is None:
            return
        self._tracer.emit(
            Kind.VERIFICATION,
            nodeid=verification.nodeid,
            targeted_outcomes=list(verification.targeted_outcomes),
            fixed=verification.fixed,
            flaky=verification.flaky,
            regressions=list(verification.regressions),
            summary=verification.summary(),
        )
