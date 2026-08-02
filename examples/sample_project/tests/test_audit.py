import gc
import warnings

from app.audit import append_entry, count_entries


def log(tmp_path):
    path = tmp_path / "audit.log"
    for i in range(3):
        append_entry(path, f"entry {i}")
    return path


def test_entries_are_appended(tmp_path):
    assert count_entries(log(tmp_path)) == 3


def test_blank_lines_are_not_counted(tmp_path):
    path = log(tmp_path)
    append_entry(path, "   ")
    assert count_entries(path) == 3


def test_counting_entries_releases_the_file_handle(tmp_path):
    path = log(tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        for _ in range(20):
            count_entries(path)
        gc.collect()

    leaked = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaked, f"leaked {len(leaked)} file handle(s)"
