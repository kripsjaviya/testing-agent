"""Fix use case: diagnose the repository, then run the loop until it settles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain.models import Localisation, SuiteResult
from ..domain.trace import Kind, Tracer
from ..ports import SuiteRunner
from .diagnose import DiagnoseRepository, Diagnosis
from .graph import DEFAULT_MAX_ATTEMPTS, DEFAULT_MIN_CONFIDENCE

log = logging.getLogger(__name__)

# Recursion guard: every node transition counts, so this must comfortably exceed
# failures x attempts x nodes-per-attempt.
GRAPH_STEP_LIMIT = 400


@dataclass(frozen=True)
class FixOutcome:
    diagnosis: Diagnosis
    fixed: tuple[str, ...]
    unfixed: tuple[dict, ...]
    final_suite: SuiteResult | None = None

    @property
    def green(self) -> bool:
        return self.final_suite is not None and self.final_suite.green


def _confidence(localisation: Localisation) -> float:
    """How strongly the ranker placed this failure -- its best candidate's score."""
    targets = localisation.patch_targets
    return targets[0].score if targets else 0.0


class FixRepository:
    """Run the full diagnose -> patch -> verify loop over a target repository."""

    def __init__(
        self,
        diagnose: DiagnoseRepository,
        graph_factory,
        runner: SuiteRunner,
        tracer: Tracer | None = None,
    ):
        self._diagnose = diagnose
        self._graph_factory = graph_factory
        self._runner = runner
        self._tracer = tracer

    def __call__(
        self,
        target: str,
        workspace: Path,
        python: str | None = None,
        timeout: int = 600,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        only: str | None = None,
        max_failures: int | None = None,
    ) -> FixOutcome:
        diagnosis = self._diagnose(target, workspace, python, timeout)
        if diagnosis.green:
            return FixOutcome(diagnosis, (), (), diagnosis.suite)

        failures = {f.nodeid: f for f in diagnosis.suite.failures}
        localisations = {loc.nodeid: loc for loc in diagnosis.localisations}

        queue, skipped = self._plan(failures, localisations, only, max_failures)

        final = {}
        if queue:
            final = self._graph_factory().invoke(
                {
                    "repo_root": str(diagnosis.repo.root),
                    "python": str(diagnosis.environment.python),
                    "timeout": timeout,
                    "max_attempts": max_attempts,
                    "min_confidence": min_confidence,
                    "baseline_failures": diagnosis.suite.failing_ids,
                    "queue": queue,
                    "failures": failures,
                    "localisations": localisations,
                    "fixed": [],
                    "unfixed": [],
                },
                {"recursion_limit": GRAPH_STEP_LIMIT},
            )

        fixed = tuple(final.get("fixed", []))
        # Nothing was committed, so rollback restored the tree byte for byte and
        # the opening suite is still an accurate measurement. Re-running it would
        # cost a full suite for no new information.
        final_suite = diagnosis.suite if not fixed else self._runner.run(
            diagnosis.repo.root, str(diagnosis.environment.python), timeout=timeout
        )

        unfixed = tuple(final.get("unfixed", [])) + tuple(skipped)
        if self._tracer is not None:
            self._tracer.emit(
                Kind.RUN_FINISHED,
                fixed=list(fixed),
                unfixed=[u["nodeid"] for u in unfixed],
                green=final_suite.green,
                summary=final_suite.summary(),
            )

        return FixOutcome(
            diagnosis=diagnosis,
            fixed=fixed,
            unfixed=unfixed,
            final_suite=final_suite,
        )

    def _plan(
        self,
        failures: dict,
        localisations: dict,
        only: str | None,
        max_failures: int | None,
    ) -> tuple[list[str], list[dict]]:
        """Decide which failures to attempt, and in what order."""
        skipped: list[dict] = []
        candidates = []

        for nodeid in failures:
            localisation = localisations.get(nodeid)
            if localisation is None or not localisation.patch_targets:
                # Nothing to send a model, so no model call is made.
                skipped.append(
                    {"nodeid": nodeid, "reason": "no patchable candidate located", "attempts": 0}
                )
            else:
                candidates.append(nodeid)

        if only:
            matched = [n for n in candidates if n == only] or [n for n in candidates if only in n]
            skipped += [
                {"nodeid": n, "reason": f"excluded by --only {only}", "attempts": 0}
                for n in candidates
                if n not in matched
            ]
            candidates = matched

        # Strongest localisation first: a failure the ranker placed confidently is
        # both likelier to be fixed and likelier to clear others when it is.
        candidates.sort(key=lambda n: -_confidence(localisations[n]))

        if max_failures is not None and len(candidates) > max_failures:
            skipped += [
                {"nodeid": n, "reason": f"beyond --max-failures {max_failures}", "attempts": 0}
                for n in candidates[max_failures:]
            ]
            candidates = candidates[:max_failures]

        return candidates, skipped
