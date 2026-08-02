"""TraceSink adapters.

JSONL rather than a single JSON array: each line is flushed as it is written, so
a trace stays complete and readable even if the run is killed mid-way -- which is
exactly when you most want to see what the agent was doing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..domain.trace import TraceEvent

log = logging.getLogger(__name__)


class JsonlTraceSink:
    """Appends one JSON object per line to a file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")

    def write(self, event: TraceEvent) -> None:
        json.dump(event.as_dict(), self._handle, default=str)
        self._handle.write("\n")
        self._handle.flush()  # survive a hard kill

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "JsonlTraceSink":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class NullTraceSink:
    """Discards events. Used when tracing is switched off."""

    def write(self, event: TraceEvent) -> None:
        return None

    def close(self) -> None:
        return None
