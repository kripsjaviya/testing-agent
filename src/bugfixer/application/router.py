"""Tier-based routing over whichever LLM provider was selected for the run.

Call sites ask for a *tier* and never name a vendor or a model, so swapping
providers is a config change. Routing is by task cost class, never by bug type:
what kind of bug a failure represents is unknown at runtime, and branching on it
would hard-code exactly the knowledge the agent must derive from test output.
"""

from __future__ import annotations

import time

from ..config import Settings
from ..domain.cost import BudgetExceeded, CostLedger
from ..domain.models import LLMResponse, Tier
from ..domain.trace import Kind, Tracer
from ..ports import LLMProvider, Refused

DEFAULT_MAX_TOKENS = 16_000

# Traces record how a decision was reached, so prompts are kept -- but truncated,
# because a full excerpt bundle would bury the reasoning in source code.
PROMPT_EXCERPT_CHARS = 2000
RESPONSE_EXCERPT_CHARS = 2000


class LLMRouter:
    """Maps a tier to a model on the selected provider, meters and records the call."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: Settings,
        ledger: CostLedger,
        tracer: Tracer | None = None,
    ):
        self._provider = provider
        self._settings = settings
        self._ledger = ledger
        self._tracer = tracer

    @property
    def ledger(self) -> CostLedger:
        return self._ledger

    def complete(
        self,
        tier: Tier,
        system: str,
        prompt: str,
        label: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        model = self._settings.model_for(tier)
        label = label or tier.value

        # Checked before spending, so the budget caps the run rather than
        # reporting it blew past the cap after the fact.
        try:
            self._ledger.assert_within_budget()
        except BudgetExceeded as error:
            self._trace(Kind.BUDGET_EXCEEDED, label=label, model=model, detail=str(error))
            raise

        started = time.monotonic()
        try:
            text, usage = self._provider.complete(model, system, prompt, max_tokens)
        except Refused as error:
            self._trace(Kind.LLM_REFUSED, label=label, model=model, detail=str(error))
            raise

        cost = self._ledger.record(self._provider.name, model, tier, label, usage)
        self._trace(
            Kind.LLM_CALL,
            label=label,
            provider=self._provider.name,
            model=model,
            tier=tier.value,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            elapsed_seconds=round(time.monotonic() - started, 2),
            system=_excerpt(system, PROMPT_EXCERPT_CHARS),
            prompt=_excerpt(prompt, PROMPT_EXCERPT_CHARS),
            response=_excerpt(text, RESPONSE_EXCERPT_CHARS),
        )

        return LLMResponse(
            text=text,
            provider=self._provider.name,
            model=model,
            tier=tier,
            usage=usage,
            cost_usd=cost,
        )

    def _trace(self, kind: str, **data) -> None:
        if self._tracer is not None:
            self._tracer.emit(kind, **data)


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [{len(text) - limit} more characters]"
