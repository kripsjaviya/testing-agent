"""Structured run trace.

Every decision the agent makes is recorded as an ordered, machine-readable event
so a reader can reconstruct *why* it believed a given root cause -- not just what
it did. Sequence numbers come from the tracer rather than from wall-clock order,
so a reader can rely on them even when events land in the same millisecond.

The clock is injected so traces are deterministic under test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


class Kind:
    """Event kinds. Constants, so a typo fails at import rather than in the trace."""

    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    REPO_ACQUIRED = "repo.acquired"
    ENVIRONMENT_READY = "environment.ready"
    SUITE_RUN = "suite.run"
    FAILURE_LOCALISED = "failure.localised"
    LLM_CALL = "llm.call"
    LLM_REFUSED = "llm.refused"
    BUDGET_EXCEEDED = "budget.exceeded"
    ERROR = "error"

    # Emitted once the patch/verify loop lands.
    PATCH_PROPOSED = "patch.proposed"
    PATCH_APPLIED = "patch.applied"
    PATCH_REVERTED = "patch.reverted"
    VERIFICATION = "verification"
    DECISION = "decision"


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    at: float
    kind: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "at": round(self.at, 3), "kind": self.kind, **self.data}


@dataclass
class Tracer:
    """Assigns ordering and timestamps, then hands the event to a sink."""

    sink: Any  # TraceSink; untyped here to keep the domain free of the ports module
    clock: Callable[[], float] = time.time
    _seq: int = field(default=0, init=False)

    def emit(self, kind: str, **data: Any) -> TraceEvent:
        self._seq += 1
        event = TraceEvent(seq=self._seq, at=self.clock(), kind=kind, data=data)
        self.sink.write(event)
        return event
