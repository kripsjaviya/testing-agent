"""Composition root: the one place adapters are bound to ports.

Nothing in `domain/` or `application/` constructs an adapter. Swapping a provider
or a test runner is a change here and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

from .adapters.jsonl_trace import JsonlTraceSink, NullTraceSink
from .adapters.local_workspace import FileSourceStore, LocalRepoAcquirer
from .adapters.pytest_runner import PytestRunner
from .adapters.uv_environment import UvEnvironmentProvider
from .application.agents import Diagnoser, Patcher, Reviewer
from .application.diagnose import DiagnoseRepository
from .application.fix import FixRepository
from .application.graph import build_graph
from .application.router import LLMRouter
from .application.verify import DEFAULT_REPEATS, Verifier
from .config import Settings
from .domain.cost import CostLedger
from .domain.trace import Tracer
from .ports import LLMProvider, TraceSink


def build_trace_sink(path: Path | None) -> TraceSink:
    return JsonlTraceSink(path) if path is not None else NullTraceSink()


def build_tracer(path: Path | None) -> Tracer:
    return Tracer(sink=build_trace_sink(path))


def build_diagnose(tracer: Tracer | None = None) -> DiagnoseRepository:
    return DiagnoseRepository(
        acquirer=LocalRepoAcquirer(),
        environments=UvEnvironmentProvider(),
        runner=PytestRunner(),
        tracer=tracer,
    )


def build_provider(settings: Settings) -> LLMProvider:
    # Imported lazily so a run using one provider never imports the other's SDK.
    if settings.provider == "anthropic":
        from .adapters.anthropic_llm import AnthropicProvider

        return AnthropicProvider(settings.api_key)

    from .adapters.gemini_llm import GeminiProvider

    return GeminiProvider(settings.api_key)


def build_ledger(settings: Settings) -> CostLedger:
    return CostLedger(max_cost_usd=settings.max_cost_usd, prices=settings.prices)


def build_router(
    settings: Settings,
    ledger: CostLedger | None = None,
    tracer: Tracer | None = None,
) -> LLMRouter:
    return LLMRouter(
        provider=build_provider(settings),
        settings=settings,
        ledger=ledger or build_ledger(settings),
        tracer=tracer,
    )


def build_fix(
    settings: Settings,
    ledger: CostLedger | None = None,
    tracer: Tracer | None = None,
    repeats: int = DEFAULT_REPEATS,
    check_regressions: bool = True,
) -> FixRepository:
    router = build_router(settings, ledger, tracer)
    runner = PytestRunner()
    store = FileSourceStore()

    def factory():
        return build_graph(
            diagnoser=Diagnoser(router),
            patcher=Patcher(router),
            reviewer=Reviewer(router),
            verifier=Verifier(
                runner, repeats=repeats, tracer=tracer, check_regressions=check_regressions
            ),
            store=store,
            tracer=tracer,
        )

    return FixRepository(
        diagnose=build_diagnose(tracer),
        graph_factory=factory,
        runner=runner,
        tracer=tracer,
    )
