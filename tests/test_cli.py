"""CLI-level tests: the green path, the failure path, and error handling."""

import json
import sys
import textwrap

import pytest

from bugfixer.cli import main
from bugfixer.domain.localise import clear_caches


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(tmp_path, target, *extra):
    """Run the CLI with the agent's own interpreter, skipping provisioning."""
    return main(
        [
            str(target),
            "--workspace",
            str(tmp_path / "ws"),
            "--python",
            sys.executable,
            *extra,
        ]
    )


def test_green_suite_exits_zero(tmp_path, capsys):
    target = tmp_path / "proj"
    write(target / "test_ok.py", "def test_a():\n    assert True\n")

    code = run(tmp_path, target)

    assert code == 0
    assert "Suite is green" in capsys.readouterr().out


def test_failing_suite_exits_one_and_ranks_candidates(tmp_path, capsys):
    target = tmp_path / "proj"
    write(
        target / "pricing.py",
        """
        def apply_discount(total, percent):
            return total - percent
        """,
    )
    write(
        target / "test_pricing.py",
        """
        from pricing import apply_discount

        def test_apply_discount():
            assert apply_discount(200, 10) == 180
        """,
    )

    code = run(tmp_path, target, "--json")

    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["green"] is False
    assert len(report["failures"]) == 1

    failure = report["failures"][0]
    assert failure["nodeid"] == "test_pricing.py::test_apply_discount"

    top = failure["candidates"][0]
    assert top["path"] == "pricing.py"
    assert top["symbol"] == "apply_discount"
    assert top["patchable"] is True
    assert top["reasons"]


def test_rendered_report_marks_test_files_as_context_only(tmp_path, capsys):
    target = tmp_path / "proj"
    write(
        target / "calc.py",
        """
        def double(n):
            return n + 2
        """,
    )
    write(
        target / "test_calc.py",
        """
        from calc import double

        def test_double():
            assert double(5) == 10
        """,
    )

    run(tmp_path, target)
    out = capsys.readouterr().out

    assert "1 failing test(s):" in out
    assert "calc.py" in out
    assert "context only" in out


def test_missing_target_reports_error_and_exits_two(tmp_path, capsys):
    code = run(tmp_path, tmp_path / "does-not-exist")

    assert code == 2
    assert "bugfixer:" in capsys.readouterr().err


def test_bad_python_path_reports_error(tmp_path, capsys):
    target = tmp_path / "proj"
    write(target / "test_ok.py", "def test_a():\n    assert True\n")

    code = main(
        [
            str(target),
            "--workspace",
            str(tmp_path / "ws"),
            "--python",
            str(tmp_path / "no-such-python"),
        ]
    )

    assert code == 2
    assert "--python does not exist" in capsys.readouterr().err
