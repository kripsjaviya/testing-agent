"""Run configuration: provider selection, tier-to-model mapping, prices, budget.

Infrastructure by nature -- this is the only module that reads the environment.
Prices are resolved here and handed to the domain ledger, so cost accounting
stays deterministic and free of hidden process state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .domain.models import ModelPrice, Tier

PROVIDERS = ("anthropic", "gemini")
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MAX_COST_USD = 5.00

TIER_MODELS: dict[str, dict[Tier, str]] = {
    "anthropic": {
        Tier.CHEAP: "claude-haiku-4-5",
        Tier.REASONING: "claude-opus-5",
    },
    "gemini": {
        # Unverified defaults -- override with BUGFIXER_GEMINI_CHEAP_MODEL /
        # BUGFIXER_GEMINI_REASONING_MODEL if these IDs are wrong for your account.
        Tier.CHEAP: "gemini-2.5-flash",
        Tier.REASONING: "gemini-2.5-pro",
    },
}

# USD per million tokens. A model absent from this table is still callable; the
# ledger records its tokens and reports the call as unpriced rather than
# inventing a number and misreporting spend against the budget.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(5.00, 25.00),
    "claude-sonnet-5": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
}

API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}


class ConfigError(RuntimeError):
    """The run is not configured well enough to start."""


@dataclass(frozen=True)
class Settings:
    provider: str
    models: dict[Tier, str]
    api_key: str
    max_cost_usd: float
    prices: dict[str, ModelPrice]

    def model_for(self, tier: Tier) -> str:
        return self.models[tier]


def load_settings(provider: str | None = None, env_file: Path | None = None) -> Settings:
    """Resolve provider, models, key, prices, and budget from args and the environment."""
    load_dotenv(env_file, override=False)

    provider = (provider or os.getenv("BUGFIXER_PROVIDER") or DEFAULT_PROVIDER).lower()
    if provider not in PROVIDERS:
        raise ConfigError(f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}")

    key_var = API_KEY_ENV[provider]
    api_key = os.getenv(key_var, "").strip()
    if not api_key:
        raise ConfigError(f"{key_var} is not set (copy .env.example to .env and fill it in)")

    prefix = f"BUGFIXER_{provider.upper()}"
    models = {
        tier: os.getenv(f"{prefix}_{tier.value.upper()}_MODEL") or default
        for tier, default in TIER_MODELS[provider].items()
    }

    return Settings(
        provider=provider,
        models=models,
        api_key=api_key,
        max_cost_usd=float(os.getenv("BUGFIXER_MAX_COST_USD", DEFAULT_MAX_COST_USD)),
        prices=build_prices(models.values()),
    )


def build_prices(models) -> dict[str, ModelPrice]:
    """Base price table plus any per-model overrides supplied via the environment."""
    prices = dict(PRICES)
    for model in models:
        override = _price_override(model)
        if override is not None:
            prices[model] = override
    return prices


def _price_override(model: str) -> ModelPrice | None:
    slug = model.replace("-", "_").replace(".", "_").upper()
    raw_in = os.getenv(f"BUGFIXER_PRICE_{slug}_INPUT")
    raw_out = os.getenv(f"BUGFIXER_PRICE_{slug}_OUTPUT")
    if raw_in and raw_out:
        return ModelPrice(float(raw_in), float(raw_out))
    return None
