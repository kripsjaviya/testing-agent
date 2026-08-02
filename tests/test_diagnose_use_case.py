"""The diagnose use case, driven entirely by fakes -- no subprocess, no network.

This is what the ports buy: the orchestration is testable in milliseconds, and
the same seams will carry the patch/verify/revert loop.
"""

import textwrap
from pathlib import Path

import pytest

from bugfixer.adapters.fakes import (
    FakeEnvironmentProvider,
    FakeRepoAcquirer,
    FakeSuiteRunner,
    failing,
    passing,
    suite,
)
from bugfixer.application.diagnose import DiagnoseRepository
from bugfixer.domain.localise import clear_caches
from bugfixer.domain.models import Frame


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def build(runner):
    return DiagnoseRepository(
        acquirer=FakeRepoAcquirer(),
        environments=FakeEnvironmentProvider(),
        runner=runner,
    )


def target_with_bug(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def double(n):\n    return n + 2\n", encoding="utf-8"
    )
    (tmp_path / "test_calc.py").write_text(
        textwrap.dedent(
            """
            from calc import double

            def test_double():
                assert double(5) == 10
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_green_suite_produces_no_localisations(tmp_path):
    diagnose = build(FakeSuiteRunner(suite(passing("test_calc.py::test_double"))))

    diagnosis = diagnose(str(tmp_path), workspace=tmp_path / "ws")

    assert diagnosis.green
    assert diagnosis.localisations == ()


def test_failure_is_localised_against_the_real_source_tree(tmp_path):
    target_with_bug(tmp_path)
    failure = failing(
        "test_calc.py::test_double",
        message="assert 7 == 10",
        frames=(Frame(path="test_calc.py", lineno=4, message="assert double(5) == 10"),),
    )
    diagnose = build(FakeSuiteRunner(suite(failure)))

    diagnosis = diagnose(str(tmp_path), workspace=tmp_path / "ws")

    assert not diagnosis.green
    assert len(diagnosis.localisations) == 1

    targets = diagnosis.localisations[0].patch_targets
    assert targets[0].path == "calc.py"
    assert targets[0].symbol == "double"


def test_localisations_line_up_with_failures(tmp_path):
    target_with_bug(tmp_path)
    runner = FakeSuiteRunner(
        suite(
            failing("test_calc.py::test_double", message="assert 7 == 10"),
            passing("test_calc.py::test_other"),
            failing("test_calc.py::test_third", message="assert 1 == 2"),
        )
    )

    diagnosis = build(runner)(str(tmp_path), workspace=tmp_path / "ws")

    assert [f.nodeid for f in diagnosis.suite.failures] == [
        loc.nodeid for loc in diagnosis.localisations
    ]


def test_supplied_interpreter_reaches_the_runner(tmp_path):
    runner = FakeSuiteRunner(suite(passing("t::a")))
    interpreter = tmp_path / "custom" / "python"

    build(runner)(str(tmp_path), workspace=tmp_path / "ws", python=str(interpreter))

    assert Path(runner.calls[0].python) == interpreter


def test_timeout_is_passed_through(tmp_path):
    runner = FakeSuiteRunner(suite(passing("t::a")))

    build(runner)(str(tmp_path), workspace=tmp_path / "ws", timeout=42)

    assert runner.calls[0].timeout == 42


def test_workspace_is_created(tmp_path):
    workspace = tmp_path / "ws" / "nested"
    build(FakeSuiteRunner(suite(passing("t::a"))))(str(tmp_path), workspace=workspace)

    assert workspace.is_dir()
