"""Verification: telling fixed from flaky from regressed.

Every case runs against a scripted runner. A race condition is impossible to
reproduce on demand against real pytest; as a scripted sequence it is trivial.
"""

from pathlib import Path

import pytest

from bugfixer.adapters.fakes import (
    FakeSuiteRunner,
    InMemoryTraceSink,
    failing,
    passing,
    suite,
)
from bugfixer.application.verify import Verifier
from bugfixer.domain.trace import Kind, Tracer

ROOT = Path(".")
NODE = "tests/test_api.py::test_discount"
OTHER = "tests/test_api.py::test_total"


def verify(runner, baseline=frozenset(), repeats=3, tracer=None):
    return Verifier(runner, repeats=repeats, tracer=tracer)(
        ROOT, "python", NODE, set(baseline)
    )


def test_consistent_pass_with_clean_suite_is_fixed():
    runner = FakeSuiteRunner(suite(passing(NODE)))

    result = verify(runner)

    assert result.fixed
    assert result.targeted_outcomes == (True, True, True)
    assert not result.flaky
    assert "no regressions" in result.summary()


def test_targeted_test_is_run_three_times_then_the_full_suite():
    runner = FakeSuiteRunner(suite(passing(NODE)))

    verify(runner)

    assert len(runner.targeted_calls) == 3
    assert len(runner.full_suite_calls) == 1
    assert runner.targeted_calls[0].node_ids == (NODE,)


def test_intermittent_pass_is_flaky_not_fixed():
    """The async-race case: passes once by luck, fails on a later run."""
    runner = FakeSuiteRunner(
        suite(passing(NODE)),
        suite(failing(NODE)),
        suite(passing(NODE)),
    )

    result = verify(runner)

    assert result.flaky
    assert not result.fixed
    assert result.targeted_outcomes == (True, False)
    assert "flaky" in result.summary()


def test_consistent_failure_stops_early():
    """No point running a failing test twice more."""
    runner = FakeSuiteRunner(suite(failing(NODE)))

    result = verify(runner)

    assert not result.fixed
    assert not result.flaky
    assert result.targeted_outcomes == (False,)
    assert len(runner.targeted_calls) == 1
    assert not runner.full_suite_calls  # never reached the regression check


def test_patch_that_breaks_another_test_is_a_regression():
    runner = FakeSuiteRunner(
        suite(passing(NODE)),
        suite(passing(NODE)),
        suite(passing(NODE)),
        suite(passing(NODE), failing(OTHER)),  # the full suite run
    )

    result = verify(runner)

    assert result.targeted_passed
    assert result.regressed
    assert not result.fixed
    assert result.regressions == (OTHER,)


def test_a_test_that_was_already_failing_is_not_a_regression():
    """Other queued bugs must not block this patch from being accepted."""
    runner = FakeSuiteRunner(
        suite(passing(NODE)),
        suite(passing(NODE)),
        suite(passing(NODE)),
        suite(passing(NODE), failing(OTHER)),
    )

    result = verify(runner, baseline={OTHER})

    assert result.fixed
    assert result.regressions == ()


def test_timeout_counts_as_not_passing():
    runner = FakeSuiteRunner(suite(passing(NODE), timed_out=True))

    result = verify(runner)

    assert not result.fixed
    assert result.targeted_outcomes == (False,)


def test_repeat_count_is_configurable():
    runner = FakeSuiteRunner(suite(passing(NODE)))

    verify(runner, repeats=5)

    assert len(runner.targeted_calls) == 5


def test_a_single_repeat_is_allowed_but_zero_is_not():
    runner = FakeSuiteRunner(suite(passing(NODE)))
    assert verify(runner, repeats=1).fixed

    with pytest.raises(ValueError):
        Verifier(runner, repeats=0)


def test_verification_is_traced():
    sink = InMemoryTraceSink()
    tracer = Tracer(sink=sink, clock=lambda: 1.0)
    runner = FakeSuiteRunner(
        suite(passing(NODE)),
        suite(failing(NODE)),
        suite(passing(NODE)),
    )

    verify(runner, tracer=tracer)

    event = sink.of_kind(Kind.VERIFICATION)[0]
    assert event.data["flaky"] is True
    assert event.data["fixed"] is False
    assert event.data["targeted_outcomes"] == [True, False]
