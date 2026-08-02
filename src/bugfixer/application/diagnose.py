"""Diagnose use case: acquire, prepare, run, localise.

Depends only on ports, so it can be driven end-to-end with in-memory fakes. The
CLI is a translation layer over this -- no orchestration lives in the entry point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain.localise import SymbolIndex, clear_caches, localise
from ..domain.models import Localisation, SuiteResult, TargetEnvironment, TargetRepo
from ..domain.trace import Kind, Tracer
from ..ports import EnvironmentProvider, RepoAcquirer, SuiteRunner

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Diagnosis:
    repo: TargetRepo
    environment: TargetEnvironment
    suite: SuiteResult
    localisations: tuple[Localisation, ...]

    @property
    def green(self) -> bool:
        return self.suite.green


class DiagnoseRepository:
    """Locate the code behind every failing test in a target repository."""

    def __init__(
        self,
        acquirer: RepoAcquirer,
        environments: EnvironmentProvider,
        runner: SuiteRunner,
        tracer: Tracer | None = None,
    ):
        self._acquirer = acquirer
        self._environments = environments
        self._runner = runner
        self._tracer = tracer

    def __call__(
        self,
        target: str,
        workspace: Path,
        python: str | None = None,
        timeout: int = 600,
    ) -> Diagnosis:
        workspace.mkdir(parents=True, exist_ok=True)
        self._trace(Kind.RUN_STARTED, target=target, workspace=str(workspace))

        repo = self._acquirer.acquire(target, workspace)
        self._trace(Kind.REPO_ACQUIRED, root=str(repo.root), cloned=repo.cloned)

        environment = self._environments.ensure(repo.root, workspace, python)
        self._trace(
            Kind.ENVIRONMENT_READY,
            python=str(environment.python),
            origin=environment.origin,
        )

        suite = self._runner.run(repo.root, str(environment.python), timeout=timeout)
        log.info("suite: %s", suite.summary())
        self._trace(
            Kind.SUITE_RUN,
            scope="full",
            summary=suite.summary(),
            exitstatus=suite.exitstatus,
            green=suite.green,
            timed_out=suite.timed_out,
            duration_seconds=round(suite.duration, 2),
            failing=suite.failing_ids,
        )

        # The target may have been patched by an earlier stage of the run.
        clear_caches()
        index = SymbolIndex(repo.root)

        localisations = []
        for failure in suite.failures:
            found = localise(repo.root, failure, index)
            localisations.append(found)
            self._trace(
                Kind.FAILURE_LOCALISED,
                nodeid=failure.nodeid,
                message=failure.message.strip(),
                candidates=[
                    {
                        "path": e.path,
                        "symbol": e.symbol,
                        "lines": [e.start_line, e.end_line],
                        "score": e.score,
                        "patchable": e.patchable,
                        "reasons": list(e.reasons),
                    }
                    for e in found.excerpts
                ],
            )

        return Diagnosis(
            repo=repo,
            environment=environment,
            suite=suite,
            localisations=tuple(localisations),
        )

    def _trace(self, kind: str, **data) -> None:
        if self._tracer is not None:
            self._tracer.emit(kind, **data)
