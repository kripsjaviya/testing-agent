"""Contract tests: every adapter must satisfy its port.

Structural conformance is checked for real adapters without constructing them
(that would need an API key or a subprocess). Behavioural contracts are checked
against the fakes, which is what the application layer is tested through -- a
fake that drifts from the real contract would make every loop test meaningless.
"""

import inspect
from pathlib import Path

import pytest

from bugfixer.adapters.anthropic_llm import AnthropicProvider
from bugfixer.adapters.fakes import (
    FakeEnvironmentProvider,
    FakeLLMProvider,
    FakeRepoAcquirer,
    FakeSuiteRunner,
    InMemorySourceStore,
    failing,
    passing,
    suite,
)
from bugfixer.adapters.gemini_llm import GeminiProvider
from bugfixer.adapters.local_workspace import FileSourceStore, LocalRepoAcquirer
from bugfixer.adapters.pytest_runner import PytestRunner
from bugfixer.adapters.uv_environment import UvEnvironmentProvider
from bugfixer.ports import (
    EnvironmentProvider,
    LLMProvider,
    RepoAcquirer,
    SourceStore,
    SuiteRunner,
)


def signature_of(obj, method):
    return inspect.signature(getattr(obj, method))


# --- structural conformance ----------------------------------------------


@pytest.mark.parametrize("adapter", [AnthropicProvider, GeminiProvider, FakeLLMProvider])
def test_llm_adapters_match_the_port_signature(adapter):
    assert signature_of(adapter, "complete") == signature_of(LLMProvider, "complete")


@pytest.mark.parametrize("adapter", [PytestRunner, FakeSuiteRunner])
def test_test_runners_match_the_port_signature(adapter):
    assert signature_of(adapter, "run") == signature_of(SuiteRunner, "run")


@pytest.mark.parametrize("adapter", [LocalRepoAcquirer, FakeRepoAcquirer])
def test_repo_acquirers_match_the_port_signature(adapter):
    assert signature_of(adapter, "acquire") == signature_of(RepoAcquirer, "acquire")


@pytest.mark.parametrize("adapter", [UvEnvironmentProvider, FakeEnvironmentProvider])
def test_environment_providers_match_the_port_signature(adapter):
    assert signature_of(adapter, "ensure") == signature_of(EnvironmentProvider, "ensure")


@pytest.mark.parametrize("adapter", [FileSourceStore, InMemorySourceStore])
def test_source_stores_satisfy_the_port(adapter):
    assert isinstance(adapter(), SourceStore)


# --- behavioural contract: SourceStore ------------------------------------


def disk_store(tmp_path):
    store = FileSourceStore()
    (tmp_path / "a.py").write_text("original", encoding="utf-8")
    return store, tmp_path / "a.py"


def memory_store(tmp_path):
    store = InMemorySourceStore()
    path = tmp_path / "a.py"
    store.files[path] = "original"
    return store, path


STORES = [disk_store, memory_store]


@pytest.mark.parametrize("make_store", STORES)
def test_source_store_rollback_restores_the_original(tmp_path, make_store):
    store, path = make_store(tmp_path)

    store.write(path, "patched")
    assert store.read(path) == "patched"

    store.rollback()
    assert store.read(path) == "original"


@pytest.mark.parametrize("make_store", STORES)
def test_source_store_commit_keeps_the_write(tmp_path, make_store):
    store, path = make_store(tmp_path)

    store.write(path, "patched")
    store.commit()
    store.rollback()

    assert store.read(path) == "patched"


@pytest.mark.parametrize("make_store", STORES)
def test_source_store_rolls_back_to_the_earliest_content(tmp_path, make_store):
    store, path = make_store(tmp_path)

    store.write(path, "v1")
    store.write(path, "v2")
    store.rollback()

    assert store.read(path) == "original"


# --- behavioural contract: SuiteRunner -------------------------------------


def test_fake_runner_replays_then_repeats_the_final_result():
    runner = FakeSuiteRunner(
        suite(failing("t::a")),
        suite(passing("t::a")),
    )

    first = runner.run(Path("."), "python")
    second = runner.run(Path("."), "python")
    third = runner.run(Path("."), "python")

    assert not first.green
    assert second.green and third.green


def test_fake_runner_separates_targeted_from_full_runs():
    runner = FakeSuiteRunner(suite(passing("t::a")))

    runner.run(Path("."), "python", node_ids=["t::a"])
    runner.run(Path("."), "python")

    assert len(runner.targeted_calls) == 1
    assert len(runner.full_suite_calls) == 1


def test_fake_runner_rejects_an_empty_script():
    with pytest.raises(ValueError):
        FakeSuiteRunner()
