"""Ports: the complete set of outside-world capabilities the agent depends on.

Every port is a Protocol over domain value objects. No SDK type, subprocess
handle, or file descriptor crosses these boundaries, so the application layer can
be driven entirely by in-memory fakes -- which is what makes the retry, revert,
and flaky-test paths testable without spawning real pytest or calling an API.

Adding a provider or swapping the test runner means writing one adapter; nothing
in `application/` or `domain/` changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .domain.models import SuiteResult, TargetEnvironment, TargetRepo, Usage
from .domain.trace import TraceEvent


class PortError(RuntimeError):
    """Base for failures originating outside the agent."""


class RepoError(PortError):
    """Target repository could not be acquired."""


class EnvironmentNotReady(PortError):
    """No interpreter capable of running the target's tests could be prepared."""


class SuiteRunError(PortError):
    """The test suite could not be executed at all."""


class LLMError(PortError):
    """A completion could not be produced."""


class RateLimited(LLMError):
    """The provider refused on quota, not on content.

    Distinct from Refused because the response differs: a rate limit may clear on
    its own, whereas a content refusal will not if the request is repeated.
    """


class Refused(LLMError):
    """The provider's safety classifiers declined the request.

    Reachable in normal operation: a planted bug may be an injection flaw, and
    asking a model to analyse one is adjacent to what cyber classifiers watch
    for. Typed separately so the agent can fall back rather than retry blindly.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """One vendor's completion endpoint."""

    name: str

    def complete(
        self, model: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage]:
        """Return the response text and token usage, or raise LLMError/Refused."""


@runtime_checkable
class SuiteRunner(Protocol):
    """Executes a target's test suite and reports structured results."""

    def run(
        self,
        root: Path,
        python: str,
        node_ids: list[str] | None = None,
        timeout: int = 600,
    ) -> SuiteResult:
        """Run the suite, optionally narrowed to ``node_ids``."""


@runtime_checkable
class RepoAcquirer(Protocol):
    """Resolves a target reference to a local checkout."""

    def acquire(self, source: str, workspace: Path) -> TargetRepo:
        """Return a local checkout for a path or clone URL."""


@runtime_checkable
class EnvironmentProvider(Protocol):
    """Supplies an interpreter able to run the target's suite."""

    def ensure(
        self, repo_root: Path, workspace: Path, python: str | None = None
    ) -> TargetEnvironment:
        """Return an interpreter, provisioning one if needed."""


@runtime_checkable
class TraceSink(Protocol):
    """Destination for run-trace events."""

    def write(self, event: TraceEvent) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class SourceStore(Protocol):
    """Reads and writes target source files, with rollback.

    The agent must be able to undo a patch exactly; this is the only port
    permitted to modify the target.
    """

    def read(self, path: Path) -> str: ...

    def write(self, path: Path, content: str) -> None: ...

    def rollback(self) -> None:
        """Restore every file written since the last commit."""

    def commit(self) -> None:
        """Keep the writes made so far; nothing further can roll them back."""
