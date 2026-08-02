"""Value objects shared across the agent.

These carry facts out of pytest and the source tree. They have no identity and no
lifecycle, so they hold only derived behaviour (filtering, rendering) -- the real
work lives in the domain services that operate on them.

Stdlib only, by rule: nothing here may import an SDK, a subprocess, or config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Tier(str, Enum):
    """What a call costs, not what it is about."""

    CHEAP = "cheap"  # parsing test output, triage, summarising diffs
    REASONING = "reasoning"  # root-cause analysis, patch generation, escalation


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    tier: Tier
    usage: Usage
    cost_usd: float | None  # None when the model has no known price


@dataclass(frozen=True)
class LedgerEntry:
    provider: str
    model: str
    tier: Tier
    label: str
    usage: Usage
    cost_usd: float | None


# --- test execution -------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    path: str
    lineno: int | None
    message: str


@dataclass(frozen=True)
class CaseReport:
    nodeid: str
    outcome: str
    phase: str | None = None
    duration: float = 0.0
    message: str = ""
    longrepr: str = ""
    frames: tuple[Frame, ...] = ()
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


@dataclass(frozen=True)
class SuiteResult:
    exitstatus: int
    tests: tuple[CaseReport, ...]
    collection_errors: tuple[dict, ...]
    duration: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def failures(self) -> list[CaseReport]:
        return [t for t in self.tests if t.failed]

    @property
    def failing_ids(self) -> list[str]:
        return [t.nodeid for t in self.failures]

    @property
    def green(self) -> bool:
        return (
            not self.timed_out
            and not self.failures
            and not self.collection_errors
            and self.exitstatus == 0
        )

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for test in self.tests:
            counts[test.outcome] = counts.get(test.outcome, 0) + 1
        parts = [f"{n} {outcome}" for outcome, n in sorted(counts.items())]
        if self.collection_errors:
            parts.append(f"{len(self.collection_errors)} collection error(s)")
        if self.timed_out:
            parts.append("TIMED OUT")
        return ", ".join(parts) or "no tests"


# --- localisation ---------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """A named, self-contained unit of source."""

    path: Path
    name: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Excerpt:
    path: str  # relative to the repo root, for stable prompts and logs
    symbol: str | None
    start_line: int
    end_line: int
    source: str
    patchable: bool
    score: float
    reasons: tuple[str, ...]

    def render(self) -> str:
        header = f"{self.path}:{self.start_line}-{self.end_line}"
        if self.symbol:
            header += f" ({self.symbol})"
        if not self.patchable:
            header += "  [test file -- context only, must not be modified]"
        return f"# {header}\n{self.source}"


@dataclass(frozen=True)
class Localisation:
    nodeid: str
    excerpts: tuple[Excerpt, ...] = ()

    @property
    def patch_targets(self) -> list[Excerpt]:
        return [e for e in self.excerpts if e.patchable]

    def render(self) -> str:
        return "\n\n".join(e.render() for e in self.excerpts)


# --- target repository ----------------------------------------------------


@dataclass(frozen=True)
class TargetRepo:
    root: Path
    source: str
    cloned: bool


@dataclass(frozen=True)
class TargetEnvironment:
    python: Path
    origin: str  # "existing" | "provisioned" | "supplied"

    def describe(self) -> str:
        return f"{self.python} ({self.origin})"
