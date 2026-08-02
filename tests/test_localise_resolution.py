"""Callee resolution: import-pinned names beat bare-name matches."""

import sys
import textwrap

import pytest

from bugfixer.adapters.pytest_runner import PytestRunner
from bugfixer.domain.localise import clear_caches, imported_modules, localise, module_path


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


def test_import_pins_a_name_defined_in_many_modules(tmp_path):
    """`serialise` exists in three modules; the import says which one is under test."""
    for module in ("alpha", "beta", "gamma"):
        write(
            tmp_path / f"{module}.py",
            f"""
            def serialise(record):
                return {{"module": "{module}"}}
            """,
        )
    write(
        tmp_path / "test_beta.py",
        """
        from beta import serialise

        def test_serialise():
            assert serialise({"id": 1}) == {"id": 1}
        """,
    )

    targets = localise(tmp_path, first_failure(tmp_path)).patch_targets

    assert targets[0].path == "beta.py"
    assert any("imported from beta" in r for r in targets[0].reasons)
    # The other two are not silently promoted alongside it.
    assert [t.path for t in targets] == ["beta.py"]


def test_ambiguous_bare_name_is_demoted_not_promoted(tmp_path):
    """A method name shared by several classes must not outrank a pinned import."""
    write(
        tmp_path / "handlers.py",
        """
        class AlphaHandler:
            def get(self, key):
                return None

        class BetaHandler:
            def get(self, key):
                return None

        class GammaHandler:
            def get(self, key):
                return None
        """,
    )
    write(
        tmp_path / "pricing.py",
        """
        def total(items):
            return sum(i["price"] for i in items) - 1
        """,
    )
    write(
        tmp_path / "test_pricing.py",
        """
        from handlers import AlphaHandler
        from pricing import total

        def test_total():
            AlphaHandler().get("k")
            assert total([{"price": 5}]) == 5
        """,
    )

    targets = localise(tmp_path, first_failure(tmp_path)).patch_targets
    by_symbol = {t.symbol: t.score for t in targets}

    assert targets[0].symbol == "total"
    assert by_symbol["total"] > by_symbol.get("get", 0)


def test_names_with_too_many_definitions_are_dropped(tmp_path):
    body = "\n\n".join(
        f"class Handler{i}:\n    def run(self):\n        return {i}" for i in range(12)
    )
    (tmp_path / "handlers.py").write_text(body + "\n", encoding="utf-8")
    write(
        tmp_path / "calc.py",
        """
        def double(n):
            return n + 2
        """,
    )
    (tmp_path / "test_calc.py").write_text(
        "from handlers import Handler0\n"
        "from calc import double\n\n\n"
        "def test_double():\n"
        "    Handler0().run()\n"
        "    assert double(5) == 10\n",
        encoding="utf-8",
    )

    targets = localise(tmp_path, first_failure(tmp_path)).patch_targets

    assert targets[0].symbol == "double"
    assert not any(t.symbol == "run" for t in targets), "12 `run` definitions should be dropped"


def test_imported_modules_maps_local_names(tmp_path):
    source = tmp_path / "t.py"
    write(
        source,
        """
        import os
        import os.path
        from pkg.transform import serialise
        from pkg.models import Row as R
        from . import sibling
        """,
    )

    mapping = imported_modules(source)

    assert mapping["serialise"] == "pkg.transform"
    assert mapping["R"] == "pkg.models"
    assert mapping["os"] == "os"
    assert "sibling" not in mapping  # relative import, not resolvable this way


def test_bom_prefixed_sources_still_parse(tmp_path):
    """Windows editors and PowerShell write UTF-8 with a BOM; ast.parse rejects it."""
    (tmp_path / "calc.py").write_text(
        "def double(n):\n    return n + 2\n", encoding="utf-8-sig"
    )
    (tmp_path / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double():\n    assert double(5) == 10\n",
        encoding="utf-8-sig",
    )

    targets = localise(tmp_path, first_failure(tmp_path)).patch_targets

    assert targets, "BOM-prefixed source produced no candidates"
    assert targets[0].symbol == "double"
    assert not targets[0].source.startswith("﻿")


def test_module_path_resolves_modules_and_packages(tmp_path):
    write(tmp_path / "pkg" / "__init__.py", "")
    write(tmp_path / "pkg" / "transform.py", "x = 1\n")

    assert module_path("pkg.transform", tmp_path).name == "transform.py"
    assert module_path("pkg", tmp_path).name == "__init__.py"
    assert module_path("nonexistent", tmp_path) is None
