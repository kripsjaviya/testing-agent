"""Repo acquisition and rollback against the local filesystem.

Rollback deliberately does *not* use ``git checkout -- .``: the target may not be
a git repo at all, and if it is, it may have pre-existing uncommitted changes
belonging to the user. Instead only the files the agent touches are snapshotted,
so a revert can never destroy work the agent did not do.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import TargetRepo
from ..ports import RepoError

log = logging.getLogger(__name__)

_URL_SCHEME = re.compile(r"^(https?://|git@|ssh://|git://)")

SOURCE_ENCODING = "utf-8-sig"


def is_url(source: str) -> bool:
    return bool(_URL_SCHEME.match(source)) or source.endswith(".git")


class LocalRepoAcquirer:
    """Resolves a path or clone URL to a checkout. Satisfies RepoAcquirer."""

    def acquire(self, source: str, workspace: Path) -> TargetRepo:
        if is_url(source):
            return self._clone(source, workspace)

        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise RepoError(f"target path does not exist or is not a directory: {root}")
        log.info("using local target repo at %s", root)
        return TargetRepo(root=root, source=source, cloned=False)

    def _clone(self, url: str, workspace: Path) -> TargetRepo:
        dest = workspace / _repo_name(url)
        # Only ever removes paths inside the agent-owned workspace directory.
        if dest.exists():
            log.info("removing previous clone at %s", dest)
            shutil.rmtree(dest, onerror=_force_remove)
        dest.parent.mkdir(parents=True, exist_ok=True)

        log.info("cloning %s -> %s", url, dest)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RepoError(f"git clone failed for {url}:\n{result.stderr.strip()}")
        return TargetRepo(root=dest, source=url, cloned=True)


def _repo_name(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def _force_remove(func, path, _exc_info):
    """shutil.rmtree onerror hook: clear the read-only bit git sets on objects."""
    Path(path).chmod(0o700)
    func(path)


@dataclass
class FileSourceStore:
    """Reads and writes target files, remembering originals for rollback.

    Capture is idempotent per path and always keeps the *earliest* content, so
    repeated edits to one file still roll back to the pre-agent state. A stored
    value of ``None`` means the file did not exist and is deleted on rollback.
    """

    _originals: dict[Path, bytes | None] = field(default_factory=dict)

    def read(self, path: Path) -> str:
        return Path(path).read_text(encoding=SOURCE_ENCODING, errors="replace")

    def write(self, path: Path, content: str) -> None:
        path = Path(path).resolve()
        self.capture(path)
        path.write_text(content, encoding="utf-8", newline="")

    def capture(self, path: Path) -> None:
        path = Path(path).resolve()
        if path in self._originals:
            return
        self._originals[path] = path.read_bytes() if path.is_file() else None

    def rollback(self) -> None:
        for path, original in self._originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        log.info("rolled back %d file(s)", len(self._originals))
        self._originals.clear()

    def commit(self) -> None:
        """Keep the edits: forget the captured originals."""
        self._originals.clear()
