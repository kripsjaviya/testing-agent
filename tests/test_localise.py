"""Localisation is tested end-to-end against real pytest output.

Fabricating CaseReport objects would let the ranker pass while the frame shapes
pytest actually emits break it -- and the whole design turns on what those frames
do and don't contain.
"""

import sys
import textwrap
from pathlib import Path

import pytest

from bugfixer.adapters.pytest_runner import PytestRunner
from bugfixer.domain.localise import (
    SymbolIndex,
    clear_caches,
    enclosing_symbol,
    failure_identifiers,
    is_test_path,
    localise,
)


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def first_failure(tmp_path):
    result = PytestRunner().run(tmp_path, python=sys.executable)
    assert result.failures, f"expected a failing test, got: {result.summary()}"
    return result.failures[0]


def test_assertion_failure_finds_source_via_callee_expansion(tmp_path):
    """The load-bearing case: the traceback names only the test file."""
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

    failure = first_failure(tmp_path)
    # Precondition for this test to mean anything.
    assert all("calc.py" not in f.path or "test_calc" in f.path for f in failure.frames)

    result = localise(tmp_path, failure)
    targets = result.patch_targets

    assert targets, "assertion failure produced no patchable candidate"
    assert targets[0].path == "calc.py"
    assert targets[0].symbol == "double"
    assert "return n + 2" in targets[0].source
    assert any("asserts on double()" in r for r in targets[0].reasons)


def test_exception_failure_ranks_source_above_test(tmp_path):
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

        def test_lookup():
            assert lookup([1, 2], 5) is None
        """,
    )

    result = localise(tmp_path, first_failure(tmp_path))

    assert result.excerpts[0].path == "service.py"
    assert result.excerpts[0].symbol == "lookup"
    test_excerpt = next(e for e in result.excerpts if e.path == "test_service.py")
    assert result.excerpts[0].score > test_excerpt.score


def test_test_files_are_never_patchable(tmp_path):
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

    result = localise(tmp_path, first_failure(tmp_path))

    assert any(e.path == "test_calc.py" for e in result.excerpts)  # present as context
    assert all(not e.patchable for e in result.excerpts if e.path == "test_calc.py")


def test_callee_resolved_across_modules(tmp_path):
    write(
        tmp_path / "pkg" / "__init__.py",
        "",
    )
    write(
        tmp_path / "pkg" / "transform.py",
        """
        def serialise(record):
            return {"id": record["id"]}
        """,
    )
    write(
        tmp_path / "test_transform.py",
        """
        from pkg.transform import serialise

        def test_serialise_keeps_name():
            assert serialise({"id": 1, "name": "x"}) == {"id": 1, "name": "x"}
        """,
    )

    result = localise(tmp_path, first_failure(tmp_path))
    targets = result.patch_targets

    assert targets[0].path == "pkg/transform.py"
    assert targets[0].symbol == "serialise"


def test_excerpt_is_a_whole_function_not_a_line_window(tmp_path):
    write(
        tmp_path / "calc.py",
        """
        HEADER = "unrelated module constant"


        def unrelated():
            return "should not appear"


        def total(values):
            running = 0
            for value in values:
                running += value
            return running - 1
        """,
    )
    write(
        tmp_path / "test_calc.py",
        """
        from calc import total

        def test_total():
            assert total([1, 2, 3]) == 6
        """,
    )

    target = localise(tmp_path, first_failure(tmp_path)).patch_targets[0]

    assert target.symbol == "total"
    assert target.source.startswith("def total(values):")
    assert "return running - 1" in target.source
    assert "unrelated" not in target.source
    assert "HEADER" not in target.source


def test_identifier_overlap_promotes_the_mentioned_symbol(tmp_path):
    write(
        tmp_path / "models.py",
        """
        def build_payload(row):
            return {"price": row.get("cost")}


        def build_header(row):
            return {"title": row.get("title")}
        """,
    )
    write(
        tmp_path / "test_models.py",
        """
        from models import build_header, build_payload

        def test_payload():
            build_header({"title": "t"})
            assert build_payload({"cost": 5})["price"] == 10
        """,
    )

    result = localise(tmp_path, first_failure(tmp_path))
    targets = [t.symbol for t in result.patch_targets]

    # Both are called by the failing test; the one the failure text names wins.
    assert targets[0] == "build_payload"
    assert "build_header" in targets


def test_ranking_respects_the_excerpt_budget(tmp_path):
    helpers = "\n\n".join(f"def helper_{i}(x):\n    return x + {i}" for i in range(12))
    (tmp_path / "helpers.py").write_text(helpers + "\n", encoding="utf-8")

    # Built without dedent: interpolating multi-line call text would break the
    # common-prefix calculation and produce an uncollectable file.
    calls = "".join(f"    helper_{i}(1)\n" for i in range(12))
    (tmp_path / "test_helpers.py").write_text(
        "from helpers import *\n\n\ndef test_all():\n" + calls + "    assert False\n",
        encoding="utf-8",
    )

    result = localise(tmp_path, first_failure(tmp_path), max_excerpts=5)

    assert len(result.excerpts) == 5


def test_frames_outside_the_repo_are_ignored(tmp_path):
    write(
        tmp_path / "test_external.py",
        """
        import json

        def test_bad_json():
            json.loads("{not json}")
        """,
    )

    result = localise(tmp_path, first_failure(tmp_path))

    # json/decoder.py frames must not become patch candidates.
    assert all(not Path(e.path).is_absolute() for e in result.excerpts)
    assert all("site-packages" not in e.path and "json" not in Path(e.path).parts
               for e in result.excerpts)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("test_api.py", True),
        ("api_test.py", True),
        ("conftest.py", True),
        ("api.py", False),
        ("latest.py", False),
    ],
)
def test_is_test_path(name, expected):
    assert is_test_path(Path(name)) is expected


def test_is_test_path_detects_tests_directory():
    assert is_test_path(Path("tests/helpers.py"))


def test_enclosing_symbol_prefers_innermost(tmp_path):
    source = tmp_path / "m.py"
    write(
        source,
        """
        class Service:
            def outer(self):
                return 1

            def inner(self):
                value = 2
                return value
        """,
    )

    symbol = enclosing_symbol(source, lineno=7)

    assert symbol is not None
    assert symbol.name == "inner"


def test_failure_identifiers_excludes_python_vocabulary():
    from bugfixer.domain.models import CaseReport

    failure = CaseReport(
        nodeid="t::t",
        outcome="failed",
        message="AttributeError: 'NoneType' object has no attribute 'discount_rate'",
        longrepr="assert compute_total(cart) == 100",
    )

    identifiers = failure_identifiers(failure)

    assert "discount_rate" in identifiers
    assert "compute_total" in identifiers
    assert "assert" not in identifiers  # keyword
    assert "object" not in identifiers  # builtin


def test_symbol_index_skips_excluded_directories(tmp_path):
    write(tmp_path / "app.py", "def real():\n    return 1\n")
    write(tmp_path / ".venv" / "lib" / "vendored.py", "def real():\n    return 2\n")

    found = SymbolIndex(tmp_path).lookup("real")

    assert len(found) == 1
    assert found[0].path.name == "app.py"
