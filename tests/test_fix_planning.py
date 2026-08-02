"""Work selection and collateral fixes.

Covers the three behaviours the design promised but the first implementation did
not have: strongest-localisation-first ordering, pruning failures that a previous
patch already cleared, and scoping a run with --only / --max-failures.
"""

import textwrap
from pathlib import Path

import pytest

from bugfixer.adapters.fakes import (
    FakeEnvironmentProvider,
    FakeLLMProvider,
    FakeRepoAcquirer,
    FakeSuiteRunner,
    InMemoryTraceSink,
    failing,
    passing,
    suite,
)
from bugfixer.adapters.local_workspace import FileSourceStore
from bugfixer.application.agents import DIAGNOSER_SYSTEM, Diagnoser, Patcher, Reviewer
from bugfixer.application.diagnose import DiagnoseRepository
from bugfixer.application.fix import FixRepository
from bugfixer.application.graph import build_graph
from bugfixer.application.router import LLMRouter
from bugfixer.application.verify import Verifier
from bugfixer.config import Settings
from bugfixer.domain.cost import CostLedger
from bugfixer.domain.localise import clear_caches
from bugfixer.domain.models import Frame, ModelPrice, Tier
from bugfixer.domain.trace import Kind, Tracer

APOSTROPHE = "tests/test_repo.py::test_apostrophes"
INJECTION = "tests/test_repo.py::test_injection"


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def target(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "repo.py").write_text(
        textwrap.dedent(
            """
            def find_by_customer(rows, customer):
                return [r for r in rows if r == customer]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_repo.py").write_text(
        textwrap.dedent(
            """
            from app.repo import find_by_customer

            def test_injection():
                assert find_by_customer([], "' OR '1'='1") == []

            def test_apostrophes():
                assert find_by_customer(["o'brien"], "o'brien") == ["o'brien"]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return tmp_path


def two_failures():
    frame = Frame(path="tests/test_repo.py", lineno=4, message="assert ...")
    return (
        failing(INJECTION, message="assert rows == []", frames=(frame,)),
        failing(
            APOSTROPHE,
            message="assert [] == [\"o'brien\"]",
            frames=(Frame(path="tests/test_repo.py", lineno=7, message="assert ..."),),
        ),
    )


PATCH = (
    "FILE: app/repo.py\n"
    "SYMBOL: find_by_customer\n"
    "RATIONALE: comparison was wrong\n"
    "```python\n"
    "def find_by_customer(rows, customer):\n"
    "    return [r for r in rows if r == customer]  # fixed\n"
    "```\n"
)
APPROVE = "CONFIDENCE: 0.9\nVERDICT: apply\nREASON: fine"


def settings():
    return Settings(
        provider="anthropic",
        models={Tier.CHEAP: "claude-haiku-4-5", Tier.REASONING: "claude-opus-5"},
        api_key="test",
        max_cost_usd=100.0,
        prices={"claude-opus-5": ModelPrice(5.0, 25.0)},
    )


def build(tmp_path, runner, replies, tracer=None):
    config = settings()
    provider = FakeLLMProvider(responses=list(replies))
    router = LLMRouter(provider, config, CostLedger(config.max_cost_usd, config.prices), tracer)
    store = FileSourceStore()

    diagnose = DiagnoseRepository(
        acquirer=FakeRepoAcquirer(),
        environments=FakeEnvironmentProvider(),
        runner=runner,
        tracer=tracer,
    )

    def factory():
        return build_graph(
            Diagnoser(router), Patcher(router), Reviewer(router),
            Verifier(runner, repeats=1, tracer=tracer), store, tracer,
        )

    return FixRepository(diagnose, factory, runner, tracer), provider


def test_one_patch_clearing_two_failures_skips_the_second_entirely(tmp_path):
    """Failing tests are not one-to-one with bugs."""
    target(tmp_path)
    sink = InMemoryTraceSink()
    tracer = Tracer(sink=sink, clock=lambda: 1.0)
    runner = FakeSuiteRunner(
        suite(*two_failures()),  # diagnosis
        suite(passing(INJECTION), passing(APOSTROPHE)),  # everything after the patch
    )
    fix, provider = build(tmp_path, runner, ["cause", PATCH, APPROVE], tracer)

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws")

    assert set(outcome.fixed) == {INJECTION, APOSTROPHE}
    assert outcome.unfixed == ()
    # Only one diagnose/patch/review cycle was spent on two failing tests.
    assert len([p for p in provider.prompts if p[1] == DIAGNOSER_SYSTEM]) == 1
    assert sink.of_kind(Kind.DECISION)


def test_strongest_localisation_is_attempted_first(tmp_path):
    target(tmp_path)
    # The second failure names the symbol in its message, so it scores higher.
    weak = failing(
        "tests/test_repo.py::test_weak",
        message="assert False",
        frames=(Frame(path="tests/test_repo.py", lineno=4, message="assert ..."),),
    )
    strong = failing(
        INJECTION,
        message="find_by_customer returned the wrong rows for customer",
        frames=(Frame(path="tests/test_repo.py", lineno=4, message="assert ..."),),
    )
    runner = FakeSuiteRunner(suite(weak, strong))
    fix, provider = build(tmp_path, runner, ["cause", "unusable"])

    fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    first_prompt = provider.prompts[0][2]
    assert "find_by_customer returned the wrong rows" in first_prompt


def test_only_restricts_the_run_to_one_failure(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(suite(*two_failures()))
    fix, provider = build(tmp_path, runner, ["cause", "unusable"])

    outcome = fix(
        str(tmp_path), workspace=tmp_path / "ws", max_attempts=1, only="test_apostrophes"
    )

    attempted = [u["nodeid"] for u in outcome.unfixed if "excluded" not in u["reason"]]
    excluded = [u["nodeid"] for u in outcome.unfixed if "excluded" in u["reason"]]
    assert attempted == [APOSTROPHE]
    assert excluded == [INJECTION]
    assert len([p for p in provider.prompts if p[1] == DIAGNOSER_SYSTEM]) == 1


def test_max_failures_caps_the_work(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(suite(*two_failures()))
    fix, provider = build(tmp_path, runner, ["cause", "unusable"])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1, max_failures=1)

    assert len([p for p in provider.prompts if p[1] == DIAGNOSER_SYSTEM]) == 1
    assert any("beyond --max-failures" in u["reason"] for u in outcome.unfixed)


def test_nothing_fixed_means_the_final_suite_is_not_re_run(tmp_path):
    """Rollback restores the tree byte for byte, so the opening suite still holds."""
    target(tmp_path)
    runner = FakeSuiteRunner(suite(*two_failures()))
    fix, _ = build(tmp_path, runner, ["cause", "unusable"])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    assert outcome.fixed == ()
    assert outcome.final_suite is outcome.diagnosis.suite
    assert len(runner.full_suite_calls) == 1  # the diagnosis run, and nothing more


def test_deferred_regression_check_skips_the_per_patch_full_suite(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(
        suite(*two_failures()),
        suite(passing(INJECTION), passing(APOSTROPHE)),
    )
    config = settings()
    provider = FakeLLMProvider(responses=["cause", PATCH, APPROVE])
    router = LLMRouter(provider, config, CostLedger(config.max_cost_usd, config.prices))
    store = FileSourceStore()
    diagnose = DiagnoseRepository(FakeRepoAcquirer(), FakeEnvironmentProvider(), runner)

    def factory():
        return build_graph(
            Diagnoser(router), Patcher(router), Reviewer(router),
            Verifier(runner, repeats=1, check_regressions=False), store, None,
        )

    outcome = FixRepository(diagnose, factory, runner)(
        str(tmp_path), workspace=tmp_path / "ws", max_attempts=1
    )

    assert INJECTION in outcome.fixed
    # Diagnosis run + closing run only -- no per-patch regression suite.
    assert len(runner.full_suite_calls) == 2
