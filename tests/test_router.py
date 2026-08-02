"""Router and ledger tests run against a fake provider -- no network, no API key."""

import pytest

from bugfixer.adapters.fakes import FakeLLMProvider
from bugfixer.application.router import LLMRouter
from bugfixer.config import Settings
from bugfixer.domain.cost import BudgetExceeded, CostLedger
from bugfixer.domain.models import ModelPrice, Tier, Usage

PRICES = {
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
    "claude-opus-5": ModelPrice(5.00, 25.00),
}


def settings(**overrides):
    base = dict(
        provider="anthropic",
        models={Tier.CHEAP: "claude-haiku-4-5", Tier.REASONING: "claude-opus-5"},
        api_key="test",
        max_cost_usd=5.0,
        prices=PRICES,
    )
    base.update(overrides)
    return Settings(**base)


def build(provider=None, **overrides):
    config = settings(**overrides)
    ledger = CostLedger(config.max_cost_usd, config.prices)
    return LLMRouter(provider or FakeLLMProvider(), config, ledger), ledger


def test_tier_selects_the_configured_model():
    provider = FakeLLMProvider()
    router, _ = build(provider)

    router.complete(Tier.CHEAP, "sys", "prompt")
    router.complete(Tier.REASONING, "sys", "prompt")

    assert [call[0] for call in provider.prompts] == ["claude-haiku-4-5", "claude-opus-5"]


def test_cost_uses_per_model_prices():
    provider = FakeLLMProvider(usage=Usage(1_000_000, 1_000_000))
    router, ledger = build(provider, max_cost_usd=100.0)

    cheap = router.complete(Tier.CHEAP, "s", "p")
    reasoning = router.complete(Tier.REASONING, "s", "p")

    assert cheap.cost_usd == pytest.approx(6.00)  # haiku 4.5: $1 in + $5 out
    assert reasoning.cost_usd == pytest.approx(30.00)  # opus 5: $5 in + $25 out
    assert ledger.total_usd == pytest.approx(36.00)


def test_identical_token_counts_cost_different_amounts_per_tier():
    """A tokens-only counter would report these two calls as identical spend."""
    router, ledger = build(FakeLLMProvider(usage=Usage(100_000, 100_000)))

    cheap = router.complete(Tier.CHEAP, "s", "p")
    reasoning = router.complete(Tier.REASONING, "s", "p")

    assert cheap.usage == reasoning.usage
    assert cheap.cost_usd < reasoning.cost_usd
    assert ledger.summary()["input_tokens"] == 200_000


def test_unknown_model_is_recorded_but_unpriced():
    router, ledger = build(
        models={Tier.CHEAP: "some-new-model", Tier.REASONING: "some-new-model"}
    )

    response = router.complete(Tier.CHEAP, "s", "p")

    assert response.cost_usd is None
    assert ledger.unpriced_calls == 1
    assert ledger.summary()["input_tokens"] == 1000  # tokens still tracked


def test_budget_stops_the_run_before_spending_more():
    provider = FakeLLMProvider(usage=Usage(1_000_000, 0))
    router, ledger = build(provider, max_cost_usd=6.0)

    router.complete(Tier.REASONING, "s", "p")  # $5.00
    assert ledger.total_usd == pytest.approx(5.00)

    router.complete(Tier.REASONING, "s", "p")  # $10.00 total -- over budget
    with pytest.raises(BudgetExceeded):
        router.complete(Tier.REASONING, "s", "p")

    assert len(ledger.entries) == 2  # the third call never reached the provider


def test_summary_groups_by_model():
    router, ledger = build()

    router.complete(Tier.CHEAP, "s", "p")
    router.complete(Tier.CHEAP, "s", "p")
    router.complete(Tier.REASONING, "s", "p")

    summary = ledger.summary()
    assert summary["calls"] == 3
    assert summary["by_model"]["claude-haiku-4-5"]["calls"] == 2
    assert summary["by_model"]["claude-opus-5"]["calls"] == 1


def test_ledger_prices_are_injected_not_read_from_the_environment(monkeypatch):
    """Cost accounting must not depend on hidden process state."""
    monkeypatch.setenv("BUGFIXER_PRICE_CLAUDE_OPUS_5_INPUT", "999")
    monkeypatch.setenv("BUGFIXER_PRICE_CLAUDE_OPUS_5_OUTPUT", "999")

    router, _ = build(FakeLLMProvider(usage=Usage(1_000_000, 0)), max_cost_usd=100.0)
    response = router.complete(Tier.REASONING, "s", "p")

    assert response.cost_usd == pytest.approx(5.00)
