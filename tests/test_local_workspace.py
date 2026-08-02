import pytest

from bugfixer.adapters.local_workspace import FileSourceStore, LocalRepoAcquirer, is_url
from bugfixer.ports import RepoError


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@host/owner/repo",
    ],
)
def test_is_url_detects_clone_sources(source):
    assert is_url(source)


@pytest.mark.parametrize("source", ["/home/me/proj", "D:\\work\\proj", "./relative"])
def test_is_url_rejects_local_paths(source):
    assert not is_url(source)


def test_acquire_local_path(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()

    repo = LocalRepoAcquirer().acquire(str(target), workspace=tmp_path / "ws")

    assert repo.root == target.resolve()
    assert repo.cloned is False


def test_acquire_missing_path_raises(tmp_path):
    with pytest.raises(RepoError):
        LocalRepoAcquirer().acquire(str(tmp_path / "nope"), workspace=tmp_path / "ws")


def test_rollback_restores_modified_file(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("original")
    store = FileSourceStore()

    store.write(f, "patched")
    store.rollback()

    assert f.read_text() == "original"


def test_rollback_deletes_file_that_did_not_exist(tmp_path):
    f = tmp_path / "new.py"
    store = FileSourceStore()

    store.write(f, "created by agent")
    store.rollback()

    assert not f.exists()


def test_repeated_edits_roll_back_to_the_original(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v0")
    store = FileSourceStore()

    store.write(f, "v1")
    store.write(f, "v2")  # second attempt on the same file
    store.rollback()

    assert f.read_text() == "v0"


def test_commit_keeps_edits(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("original")
    store = FileSourceStore()

    store.write(f, "patched")
    store.commit()
    store.rollback()  # no-op after commit

    assert f.read_text() == "patched"


def test_read_strips_a_byte_order_mark(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8-sig")

    assert FileSourceStore().read(f) == "x = 1\n"


def test_write_does_not_translate_newlines(tmp_path):
    """A patch must not rewrite every line ending on the files it touches."""
    f = tmp_path / "a.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    store = FileSourceStore()

    store.write(f, "a = 1\nb = 3\n")

    assert f.read_bytes() == b"a = 1\nb = 3\n"
