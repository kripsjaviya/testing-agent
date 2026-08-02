"""In-memory adapters for every port.

These let the application layer -- retries, reverts, flaky-test handling, budget
stops -- be exercised deterministically, with no subprocess and no network. A
flaky test is simply a runner scripted to fail, pass, pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import (
    CaseReport,
    Frame,
    SuiteResult,
    TargetEnvironment,
    TargetRepo,
    Usage,
)
from ..ports import LLMError, RepoError


# --- builders -------------------------------------------------------------


def passing(nodeid: str) -> CaseReport:
    return CaseReport(nodeid=nodeid, outcome="passed", phase="call")


def failing(
    nodeid: str,
    message: str = "assert 1 == 2",
    frames: tuple[Frame, ...] = (),
    longrepr: str = "",
) -> CaseReport:
    return CaseReport(
        nodeid=nodeid,
        outcome="failed",
        phase="call",
        message=message,
        longrepr=longrepr or message,
        frames=frames,
    )


def suite(*tests: CaseReport, timed_out: bool = False) -> SuiteResult:
    failed = any(t.failed for t in tests)
    return SuiteResult(
        exitstatus=1 if failed else 0,
        tests=tuple(tests),
        collection_errors=(),
        duration=0.01,
        timed_out=timed_out,
    )


# --- fakes ----------------------------------------------------------------


@dataclass(frozen=True)
class RunCall:
    root: Path
    python: str
    node_ids: tuple[str, ...]
    timeout: int


class FakeSuiteRunner:
    """Replays scripted results; the final one repeats for any further calls."""

    def __init__(self, *results: SuiteResult):
        if not results:
            raise ValueError("FakeSuiteRunner needs at least one scripted result")
        self._results = list(results)
        self.calls: list[RunCall] = []

    def run(
        self,
        root: Path,
        python: str,
        node_ids: list[str] | None = None,
        timeout: int = 600,
    ) -> SuiteResult:
        self.calls.append(RunCall(Path(root), python, tuple(node_ids or ()), timeout))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    @property
    def targeted_calls(self) -> list[RunCall]:
        return [c for c in self.calls if c.node_ids]

    @property
    def full_suite_calls(self) -> list[RunCall]:
        return [c for c in self.calls if not c.node_ids]


@dataclass
class FakeLLMProvider:
    """Returns scripted completions; the final one repeats."""

    name: str = "fake"
    responses: list[str] = field(default_factory=lambda: ["ok"])
    usage: Usage = Usage(1000, 500)
    error: Exception | None = None
    prompts: list[tuple[str, str, str, int]] = field(default_factory=list)

    def complete(
        self, model: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage]:
        self.prompts.append((model, system, prompt, max_tokens))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise LLMError("FakeLLMProvider ran out of scripted responses")
        text = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return text, self.usage


@dataclass
class FakeRepoAcquirer:
    root: Path | None = None
    cloned: bool = False

    def acquire(self, source: str, workspace: Path) -> TargetRepo:
        root = self.root or Path(source)
        if not root.is_dir():
            raise RepoError(f"target path does not exist: {root}")
        return TargetRepo(root=root.resolve(), source=source, cloned=self.cloned)


@dataclass
class FakeEnvironmentProvider:
    python: str = "python"
    origin: str = "supplied"

    def ensure(
        self, repo_root: Path, workspace: Path, python: str | None = None
    ) -> TargetEnvironment:
        return TargetEnvironment(Path(python or self.python), self.origin)


@dataclass
class InMemoryTraceSink:
    """Collects events so tests can assert on what the agent recorded."""

    events: list = field(default_factory=list)
    closed: bool = False

    def write(self, event) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of_kind(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


@dataclass
class InMemorySourceStore:
    """Source files held in memory, with the same rollback contract as disk."""

    files: dict[Path, str] = field(default_factory=dict)
    _originals: dict[Path, str | None] = field(default_factory=dict)

    def read(self, path: Path) -> str:
        return self.files[Path(path)]

    def write(self, path: Path, content: str) -> None:
        path = Path(path)
        self._originals.setdefault(path, self.files.get(path))
        self.files[path] = content

    def rollback(self) -> None:
        for path, original in self._originals.items():
            if original is None:
                self.files.pop(path, None)
            else:
                self.files[path] = original
        self._originals.clear()

    def commit(self) -> None:
        self._originals.clear()
