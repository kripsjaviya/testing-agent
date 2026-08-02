"""Frames whose recorded path is outside the repo.

Regression test for a real failure: a copied checkout carried stale
``__pycache__`` bytecode, each ``.pyc`` embedding the absolute path it was
compiled from. Tracebacks then pointed at a directory the agent was not working
in, and *every* failure in the run localised to nothing.
"""

from pathlib import Path

import pytest

from bugfixer.domain.localise import _resolve, clear_caches, localise
from bugfixer.domain.models import CaseReport, Frame


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    clear_caches()
    yield
    clear_caches()


def build(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pagination.py").write_text(
        "def page_count(total, per_page):\n    return total // per_page\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pagination.py").write_text(
        "from app.pagination import page_count\n\n\n"
        "def test_partial_page():\n    assert page_count(10, 3) == 4\n",
        encoding="utf-8",
    )
    return tmp_path


def test_frame_recorded_at_a_stale_absolute_path_is_relocated(tmp_path):
    root = build(tmp_path)
    stale = r"D:\somewhere\else\entirely\tests\test_pagination.py"

    resolved = _resolve(stale, root)

    assert resolved == (root / "tests" / "test_pagination.py").resolve()


def test_relocation_requires_more_than_a_bare_filename(tmp_path):
    """A shared filename alone must not pull in an unrelated file."""
    root = build(tmp_path)

    assert _resolve(r"C:\python\lib\json\decoder.py", root) is None
    assert _resolve(r"C:\elsewhere\pagination.py", root) is None


def test_a_path_genuinely_outside_the_repo_still_resolves_to_nothing(tmp_path):
    root = build(tmp_path)

    assert _resolve(r"C:\python\lib\site-packages\pytest\runner.py", root) is None


def test_localisation_survives_a_relocated_checkout(tmp_path):
    """End to end: the ranker still finds the source despite stale frame paths."""
    root = build(tmp_path)
    failure = CaseReport(
        nodeid="tests/test_pagination.py::test_partial_page",
        outcome="failed",
        phase="call",
        message="assert 3 == 4",
        longrepr="assert page_count(10, 3) == 4",
        frames=(
            Frame(
                path=r"D:\old\checkout\tests\test_pagination.py",
                lineno=5,
                message="assert page_count(10, 3) == 4",
            ),
        ),
    )

    targets = localise(root, failure).patch_targets

    assert targets, "relocated frame produced no candidates"
    assert targets[0].path == "app/pagination.py"
    assert targets[0].symbol == "page_count"


def test_direct_resolution_still_wins(tmp_path):
    """A correct path must not be dragged through the relocation fallback."""
    root = build(tmp_path)

    resolved = _resolve("app/pagination.py", root)

    assert resolved == (root / "app" / "pagination.py").resolve()
