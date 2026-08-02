"""Token and cost accounting.

Prices are injected rather than read from the environment here, so the ledger is
deterministic and testable without touching config or process state -- and so the
domain layer keeps its no-external-dependencies rule.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import LedgerEntry, ModelPrice, Tier, Usage

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """The run has spent its allowance."""


@dataclass
class CostLedger:
    """Per-call token and cost accounting, with a hard budget stop."""

    max_cost_usd: float
    prices: Mapping[str, ModelPrice] = field(default_factory=dict)
    entries: list[LedgerEntry] = field(default_factory=list)

    def record(
        self, provider: str, model: str, tier: Tier, label: str, usage: Usage
    ) -> float | None:
        price = self.prices.get(model)
        cost = None
        if price is not None:
            cost = (
                usage.input_tokens * price.input_per_mtok
                + usage.output_tokens * price.output_per_mtok
            ) / 1_000_000

        self.entries.append(LedgerEntry(provider, model, tier, label, usage, cost))
        log.debug(
            "%s/%s [%s] in=%d out=%d cost=%s",
            provider,
            model,
            label,
            usage.input_tokens,
            usage.output_tokens,
            "unknown" if cost is None else f"${cost:.4f}",
        )
        return cost

    @property
    def total_usd(self) -> float:
        return sum(e.cost_usd or 0.0 for e in self.entries)

    @property
    def unpriced_calls(self) -> int:
        return sum(1 for e in self.entries if e.cost_usd is None)

    def assert_within_budget(self) -> None:
        if self.total_usd >= self.max_cost_usd:
            raise BudgetExceeded(
                f"spent ${self.total_usd:.2f} of ${self.max_cost_usd:.2f} budget"
            )

    def summary(self) -> dict:
        return {
            "calls": len(self.entries),
            "input_tokens": sum(e.usage.input_tokens for e in self.entries),
            "output_tokens": sum(e.usage.output_tokens for e in self.entries),
            "total_usd": round(self.total_usd, 4),
            "max_cost_usd": self.max_cost_usd,
            "unpriced_calls": self.unpriced_calls,
            "by_model": self._by_model(),
        }

    def _by_model(self) -> dict[str, dict]:
        grouped: dict[str, dict] = {}
        for entry in self.entries:
            bucket = grouped.setdefault(
                entry.model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0},
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += entry.usage.input_tokens
            bucket["output_tokens"] += entry.usage.output_tokens
            bucket["usd"] = round(bucket["usd"] + (entry.cost_usd or 0.0), 4)
        return grouped
