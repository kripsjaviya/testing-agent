"""Runner tests execute a real nested pytest session in a temp directory.

The throwaway files below are unit-test fixtures for the runner's parsing, not a
stand-in target repo -- the agent itself assumes nothing about them.
"""

import sys
import textwrap

import pytest

from bugfixer.adapters.pytest_runner import PytestRunner


def write(path, body):
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(tmp_path, **kwargs):
    return PytestRunner().run(tmp_path, python=sys.executable, **kwargs)


def test_green_suite(tmp_path):
    write(
        tmp_path / "test_ok.py",
        """
        def test_a():
            assert 1 + 1 == 2

        def test_b():
            assert "x" in "axb"
        """,
    )

    result = run(tmp_path)

    assert result.green
    assert result.failures == []
    assert len(result.tests) == 2


def test_failure_captures_message_and_source_frames(tmp_path):
    write(
        tmp_path / "calc.py",
        """
        def double(n):
            return n + 2
        """,
    )
    write(
        tmp_path / "test_calc.py",
        """
        from calc import double

        def test_double():
            assert double(5) == 10
        """,
    )

    result = run(tmp_path)

    assert not result.green
    assert result.failing_ids == ["test_calc.py::test_double"]

    failure = result.failures[0]
    assert failure.phase == "call"
    assert "assert" in failure.message
    assert "test_calc.py" in failure.longrepr
    assert any("test_calc.py" in frame.path for frame in failure.frames)


def test_error_inside_source_module_points_at_that_file(tmp_path):
    write(
        tmp_path / "service.py",
        """
        def lookup(items, index):
            return items[index]
        """,
    )
    write(
        tmp_path / "test_service.py",
        """
        from service import lookup

        def test_lookup_out_of_range():
            assert lookup([1, 2], 5) is None
        """,
    )

    result = run(tmp_path)

    failure = result.failures[0]
    assert "IndexError" in failure.message
    # The frame chain must reach the source file, not just the test file --
    # this is what localisation ranks on.
    assert any("service.py" in frame.path for frame in failure.frames)


def test_teardown_failure_marks_test_failed(tmp_path):
    write(
        tmp_path / "test_teardown.py",
        """
        import pytest

        @pytest.fixture
        def leaky():
            yield 1
            raise RuntimeError("resource not released")

        def test_uses_fixture(leaky):
            assert leaky == 1
        """,
    )

    result = run(tmp_path)

    assert not result.green
    failure = result.failures[0]
    assert failure.phase == "teardown"
    assert "resource not released" in failure.longrepr


def test_collection_error_is_reported(tmp_path):
    write(tmp_path / "test_broken.py", "def test_x(:\n    pass\n")

    result = run(tmp_path)

    assert not result.green
    assert result.collection_errors
    assert "test_broken.py" in result.collection_errors[0]["nodeid"]


def test_node_ids_narrow_the_run(tmp_path):
    write(
        tmp_path / "test_many.py",
        """
        def test_one():
            assert False

        def test_two():
            assert False
        """,
    )

    result = run(tmp_path, node_ids=["test_many.py::test_one"])

    assert len(result.tests) == 1
    assert result.failing_ids == ["test_many.py::test_one"]


def test_timeout_is_reported_not_raised(tmp_path):
    write(
        tmp_path / "test_slow.py",
        """
        import time

        def test_hangs():
            time.sleep(30)
        """,
    )

    result = run(tmp_path, timeout=3)

    assert result.timed_out
    assert not result.green


def test_no_tests_collected_is_not_an_error(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing here")

    result = run(tmp_path)

    assert result.tests == ()
    assert not result.green


@pytest.mark.parametrize("marker_file", [".pytest_cache", "report.json"])
def test_target_directory_is_left_clean(tmp_path, marker_file):
    write(tmp_path / "test_ok.py", "def test_a():\n    assert True\n")

    run(tmp_path)

    assert not (tmp_path / marker_file).exists()
