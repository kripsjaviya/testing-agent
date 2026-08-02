"""The full loop, driven by scripted models and a scripted runner.

Every path through the coordinator -- accept, reject, revert, retry, exhaust,
regress -- is exercised without a network call or a subprocess, which is the whole
point of putting ports at the boundaries.
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

NODE = "test_pricing.py::test_apply_discount"


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def target(tmp_path):
    (tmp_path / "pricing.py").write_text(
        "def apply_discount(total, percent):\n    return total - percent\n", encoding="utf-8"
    )
    (tmp_path / "test_pricing.py").write_text(
        textwrap.dedent(
            """
            from pricing import apply_discount

            def test_apply_discount():
                assert apply_discount(200, 10) == 180
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return tmp_path


def bug_failure():
    return failing(
        NODE,
        message="assert 190 == 180",
        frames=(Frame(path="test_pricing.py", lineno=4, message="assert ..."),),
    )


GOOD_PATCH = (
    "FILE: pricing.py\n"
    "SYMBOL: apply_discount\n"
    "RATIONALE: percent was subtracted as an absolute amount\n"
    "```python\n"
    "def apply_discount(total, percent):\n"
    "    return total * (1 - percent / 100)\n"
    "```\n"
)

APPROVE = "CONFIDENCE: 0.9\nVERDICT: apply\nREASON: matches the stated cause"
REJECT = "CONFIDENCE: 0.2\nVERDICT: reject\nREASON: hard-codes the expected value"


def settings():
    return Settings(
        provider="anthropic",
        models={Tier.CHEAP: "claude-haiku-4-5", Tier.REASONING: "claude-opus-5"},
        api_key="test",
        max_cost_usd=100.0,
        prices={"claude-opus-5": ModelPrice(5.0, 25.0)},
    )


def build_fix(tmp_path, runner, replies, tracer=None, store=None, **graph_kwargs):
    config = settings()
    provider = FakeLLMProvider(responses=list(replies))
    router = LLMRouter(provider, config, CostLedger(config.max_cost_usd, config.prices), tracer)
    store = store or FileSourceStore()

    diagnose = DiagnoseRepository(
        acquirer=FakeRepoAcquirer(),
        environments=FakeEnvironmentProvider(),
        runner=runner,
        tracer=tracer,
    )
    factory = lambda: build_graph(  # noqa: E731 - a one-line factory reads fine here
        Diagnoser(router),
        Patcher(router),
        Reviewer(router),
        Verifier(runner, repeats=graph_kwargs.pop("repeats", 3), tracer=tracer),
        store,
        tracer,
    )
    fix = FixRepository(diagnose, factory, runner, tracer)
    return fix, provider, store


def test_green_repository_does_nothing(tmp_path):
    runner = FakeSuiteRunner(suite(passing(NODE)))
    fix, provider, _ = build_fix(tmp_path, runner, ["unused"])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws")

    assert outcome.green
    assert outcome.fixed == ()
    assert provider.prompts == []  # no model was ever called


def test_happy_path_applies_the_patch_and_reports_green(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(
        suite(bug_failure()),  # initial diagnosis
        suite(passing(NODE)),  # everything after the patch
    )
    fix, _, _ = build_fix(
        tmp_path, runner, ["percent is subtracted directly", GOOD_PATCH, APPROVE]
    )

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws")

    assert outcome.fixed == (NODE,)
    assert outcome.unfixed == ()
    assert outcome.green
    assert "total * (1 - percent / 100)" in (tmp_path / "pricing.py").read_text()


def test_rejected_patch_is_never_written_to_disk(tmp_path):
    target(tmp_path)
    original = (tmp_path / "pricing.py").read_text()
    runner = FakeSuiteRunner(suite(bug_failure()))
    fix, _, _ = build_fix(
        tmp_path,
        runner,
        ["cause", GOOD_PATCH, REJECT],  # reviewer rejects every time
        max_attempts=1,
    )

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    assert outcome.fixed == ()
    assert (tmp_path / "pricing.py").read_text() == original


def test_patch_that_fails_verification_is_reverted(tmp_path):
    target(tmp_path)
    original = (tmp_path / "pricing.py").read_text()
    # The patch applies, but the targeted test still fails afterwards.
    runner = FakeSuiteRunner(suite(bug_failure()))
    fix, _, _ = build_fix(tmp_path, runner, ["cause", GOOD_PATCH, APPROVE])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    assert outcome.fixed == ()
    assert outcome.unfixed[0]["nodeid"] == NODE
    assert (tmp_path / "pricing.py").read_text() == original, "patch was left applied"


def test_regression_causes_a_revert(tmp_path):
    target(tmp_path)
    original = (tmp_path / "pricing.py").read_text()
    other = "test_pricing.py::test_other"
    runner = FakeSuiteRunner(
        suite(bug_failure()),  # diagnosis
        suite(passing(NODE)),  # targeted x3
        suite(passing(NODE)),
        suite(passing(NODE)),
        suite(passing(NODE), failing(other)),  # full suite: broke something else
    )
    fix, _, _ = build_fix(tmp_path, runner, ["cause", GOOD_PATCH, APPROVE])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    assert outcome.fixed == ()
    assert "regressed" in outcome.unfixed[0]["reason"]
    assert (tmp_path / "pricing.py").read_text() == original


def test_patch_outside_the_candidate_set_is_refused(tmp_path):
    """The model must not be able to edit a test file or an unrelated one."""
    target(tmp_path)
    original = (tmp_path / "test_pricing.py").read_text()
    sneaky = (
        "FILE: test_pricing.py\n"
        "SYMBOL: test_apply_discount\n"
        "RATIONALE: relax the assertion\n"
        "```python\n"
        "def test_apply_discount():\n"
        "    assert True\n"
        "```\n"
    )
    runner = FakeSuiteRunner(suite(bug_failure()))
    fix, _, _ = build_fix(tmp_path, runner, ["cause", sneaky, APPROVE])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    assert outcome.fixed == ()
    assert "not an editable candidate" in outcome.unfixed[0]["history"][0]
    assert (tmp_path / "test_pricing.py").read_text() == original


def test_attempts_are_bounded_and_the_run_continues(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(suite(bug_failure()))
    fix, provider, _ = build_fix(tmp_path, runner, ["cause", GOOD_PATCH, REJECT])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=2)

    assert outcome.unfixed[0]["attempts"] == 2
    assert len(outcome.unfixed[0]["history"]) == 2
    # Exactly two diagnosis rounds -- the attempt budget, not an unbounded loop.
    diagnoses = [p for p in provider.prompts if p[1] == DIAGNOSER_SYSTEM]
    assert len(diagnoses) == 2


def test_failure_context_is_fed_back_into_the_retry(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(suite(bug_failure()))
    fix, provider, _ = build_fix(tmp_path, runner, ["cause", GOOD_PATCH, REJECT])

    fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=2)

    second_diagnosis = provider.prompts[3][2]
    assert "Previous attempts on this failure did not work" in second_diagnosis
    assert "reviewer rejected" in second_diagnosis


def test_unusable_model_reply_is_retried_not_crashed(tmp_path):
    target(tmp_path)
    runner = FakeSuiteRunner(suite(bug_failure()))
    fix, _, _ = build_fix(tmp_path, runner, ["cause", "I am not sure what to change."])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws", max_attempts=1)

    assert outcome.fixed == ()
    assert "no usable patch" in outcome.unfixed[0]["history"][0]


def test_failure_without_a_patchable_candidate_is_reported_not_attempted(tmp_path):
    """Nothing to send a model, so no model call is made."""
    runner = FakeSuiteRunner(suite(failing(NODE, message="collection blew up")))
    fix, provider, _ = build_fix(tmp_path, runner, ["unused"])

    outcome = fix(str(tmp_path), workspace=tmp_path / "ws")

    assert outcome.fixed == ()
    assert outcome.unfixed[0]["reason"] == "no patchable candidate located"
    assert provider.prompts == []


def test_the_loop_is_fully_traced(tmp_path):
    target(tmp_path)
    sink = InMemoryTraceSink()
    tracer = Tracer(sink=sink, clock=lambda: 1.0)
    runner = FakeSuiteRunner(suite(bug_failure()), suite(passing(NODE)))
    fix, _, _ = build_fix(
        tmp_path, runner, ["cause", GOOD_PATCH, APPROVE], tracer=tracer
    )

    fix(str(tmp_path), workspace=tmp_path / "ws")

    kinds = sink.kinds()
    for expected in (
        Kind.FAILURE_LOCALISED,
        Kind.LLM_CALL,
        Kind.PATCH_PROPOSED,
        Kind.PATCH_APPLIED,
        Kind.VERIFICATION,
        Kind.DECISION,
        Kind.RUN_FINISHED,
    ):
        assert expected in kinds, f"{expected} missing from trace"

    proposed = sink.of_kind(Kind.PATCH_PROPOSED)[0]
    assert proposed.data["diff"].startswith("--- a/pricing.py")


def test_cheap_tier_reviews_and_reasoning_tier_diagnoses(tmp_path):
    """Routing is by task cost class, and it must actually reach the provider."""
    target(tmp_path)
    runner = FakeSuiteRunner(suite(bug_failure()), suite(passing(NODE)))
    fix, provider, _ = build_fix(tmp_path, runner, ["cause", GOOD_PATCH, APPROVE])

    fix(str(tmp_path), workspace=tmp_path / "ws")

    models = [call[0] for call in provider.prompts]
    assert models == ["claude-opus-5", "claude-opus-5", "claude-haiku-4-5"]
