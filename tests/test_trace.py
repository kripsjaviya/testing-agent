"""Trace tests: ordering, durability, and what the agent actually records."""

import json

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
from bugfixer.adapters.jsonl_trace import JsonlTraceSink, NullTraceSink
from bugfixer.application.diagnose import DiagnoseRepository
from bugfixer.application.router import LLMRouter
from bugfixer.config import Settings
from bugfixer.domain.cost import CostLedger
from bugfixer.domain.localise import clear_caches
from bugfixer.domain.models import Frame, ModelPrice, Tier, Usage
from bugfixer.domain.trace import Kind, Tracer
from bugfixer.ports import Refused


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def tracer_with_sink():
    sink = InMemoryTraceSink()
    clock = iter(range(1000, 2000))
    return Tracer(sink=sink, clock=lambda: float(next(clock))), sink


# --- tracer ---------------------------------------------------------------


def test_sequence_numbers_are_monotonic_regardless_of_clock():
    sink = InMemoryTraceSink()
    tracer = Tracer(sink=sink, clock=lambda: 42.0)  # every event same timestamp

    tracer.emit("a")
    tracer.emit("b")
    tracer.emit("c")

    assert [e.seq for e in sink.events] == [1, 2, 3]
    assert {e.at for e in sink.events} == {42.0}


def test_event_data_is_flattened_for_serialisation():
    tracer, sink = tracer_with_sink()

    tracer.emit(Kind.SUITE_RUN, summary="1 failed", green=False)

    assert sink.events[0].as_dict() == {
        "seq": 1,
        "at": 1000.0,
        "kind": "suite.run",
        "summary": "1 failed",
        "green": False,
    }


# --- sinks ----------------------------------------------------------------


def test_jsonl_sink_writes_one_object_per_line(tmp_path):
    path = tmp_path / "nested" / "trace.jsonl"
    with JsonlTraceSink(path) as sink:
        tracer = Tracer(sink=sink, clock=lambda: 1.0)
        tracer.emit("first", value=1)
        tracer.emit("second", value=2)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["first", "second"]
    assert json.loads(lines[1])["value"] == 2


def test_jsonl_sink_flushes_so_a_killed_run_still_has_a_trace(tmp_path):
    """The trace must be readable without closing -- a hung run is when you need it."""
    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    Tracer(sink=sink, clock=lambda: 1.0).emit("mid.run", detail="still going")

    assert "mid.run" in path.read_text(encoding="utf-8")
    sink.close()


def test_null_sink_discards_everything():
    sink = NullTraceSink()
    Tracer(sink=sink).emit("ignored")
    sink.close()  # must not raise


# --- what the agent records ----------------------------------------------


def settings():
    return Settings(
        provider="anthropic",
        models={Tier.CHEAP: "claude-haiku-4-5", Tier.REASONING: "claude-opus-5"},
        api_key="test",
        max_cost_usd=5.0,
        prices={"claude-opus-5": ModelPrice(5.0, 25.0)},
    )


def test_llm_calls_are_traced_with_tokens_and_cost():
    tracer, sink = tracer_with_sink()
    config = settings()
    router = LLMRouter(
        FakeLLMProvider(usage=Usage(1_000_000, 0)),
        config,
        CostLedger(config.max_cost_usd, config.prices),
        tracer,
    )

    router.complete(Tier.REASONING, "sys", "why did this fail?", label="diagnose")

    event = sink.of_kind(Kind.LLM_CALL)[0]
    assert event.data["label"] == "diagnose"
    assert event.data["model"] == "claude-opus-5"
    assert event.data["input_tokens"] == 1_000_000
    assert event.data["cost_usd"] == pytest.approx(5.0)
    assert event.data["prompt"] == "why did this fail?"


def test_refusal_is_traced_distinctly_from_a_successful_call():
    tracer, sink = tracer_with_sink()
    config = settings()
    provider = FakeLLMProvider(error=Refused("declined"))
    router = LLMRouter(provider, config, CostLedger(config.max_cost_usd, config.prices), tracer)

    with pytest.raises(Refused):
        router.complete(Tier.REASONING, "sys", "prompt")

    assert sink.kinds() == [Kind.LLM_REFUSED]
    assert not sink.of_kind(Kind.LLM_CALL)


def test_long_prompts_are_truncated_in_the_trace():
    tracer, sink = tracer_with_sink()
    config = settings()
    router = LLMRouter(
        FakeLLMProvider(), config, CostLedger(config.max_cost_usd, config.prices), tracer
    )

    router.complete(Tier.REASONING, "sys", "x" * 5000)

    recorded = sink.of_kind(Kind.LLM_CALL)[0].data["prompt"]
    assert len(recorded) < 5000
    assert "more characters" in recorded


def test_diagnose_records_the_full_decision_path(tmp_path):
    (tmp_path / "calc.py").write_text("def double(n):\n    return n + 2\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double():\n    assert double(5) == 10\n",
        encoding="utf-8",
    )
    tracer, sink = tracer_with_sink()
    failure = failing(
        "test_calc.py::test_double",
        message="assert 7 == 10",
        frames=(Frame(path="test_calc.py", lineno=5, message="assert double(5) == 10"),),
    )
    diagnose = DiagnoseRepository(
        acquirer=FakeRepoAcquirer(),
        environments=FakeEnvironmentProvider(),
        runner=FakeSuiteRunner(suite(failure)),
        tracer=tracer,
    )

    diagnose(str(tmp_path), workspace=tmp_path / "ws")

    assert sink.kinds() == [
        Kind.RUN_STARTED,
        Kind.REPO_ACQUIRED,
        Kind.ENVIRONMENT_READY,
        Kind.SUITE_RUN,
        Kind.FAILURE_LOCALISED,
    ]

    localised = sink.of_kind(Kind.FAILURE_LOCALISED)[0]
    top = localised.data["candidates"][0]
    assert top["path"] == "calc.py"
    # The reasons are the audit trail: why the agent believed this root cause.
    assert top["reasons"]


def test_green_run_records_no_localisations(tmp_path):
    tracer, sink = tracer_with_sink()
    diagnose = DiagnoseRepository(
        acquirer=FakeRepoAcquirer(),
        environments=FakeEnvironmentProvider(),
        runner=FakeSuiteRunner(suite(passing("t::a"))),
        tracer=tracer,
    )

    diagnose(str(tmp_path), workspace=tmp_path / "ws")

    assert not sink.of_kind(Kind.FAILURE_LOCALISED)
    assert sink.of_kind(Kind.SUITE_RUN)[0].data["green"] is True


def test_diagnose_without_a_tracer_still_works(tmp_path):
    """Tracing is optional wiring, not a hard dependency of the use case."""
    diagnose = DiagnoseRepository(
        acquirer=FakeRepoAcquirer(),
        environments=FakeEnvironmentProvider(),
        runner=FakeSuiteRunner(suite(passing("t::a"))),
    )

    assert diagnose(str(tmp_path), workspace=tmp_path / "ws").green
