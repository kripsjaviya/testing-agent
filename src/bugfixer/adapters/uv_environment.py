"""EnvironmentProvider adapter: reuse the target's virtualenv, or build one with uv.

Provisioned environments live in the agent's workspace, never inside the target:
the agent should not leave a ``.venv`` behind in a repo the user pointed it at.
The interpreter is always invoked by absolute path, so the target's pinned
versions win over the agent's own dependencies.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..domain.models import TargetEnvironment
from ..ports import EnvironmentNotReady

log = logging.getLogger(__name__)

VENV_DIRS = (".venv", "venv", "env")
DEPENDENCY_FILES = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py")
INSTALL_TIMEOUT = 900


def _venv_python(venv_dir: Path) -> Path | None:
    for relative in ("Scripts/python.exe", "bin/python"):
        candidate = venv_dir / relative
        if candidate.is_file():
            return candidate
    return None


def discover(repo_root: Path) -> Path | None:
    """Find a virtualenv interpreter already present in the target."""
    for name in VENV_DIRS:
        python = _venv_python(repo_root / name)
        if python is not None:
            log.debug("found existing interpreter at %s", python)
            return python
    return None


def has_pytest(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def dependency_files(repo_root: Path) -> list[Path]:
    return [repo_root / name for name in DEPENDENCY_FILES if (repo_root / name).is_file()]


class UvEnvironmentProvider:
    """Satisfies the EnvironmentProvider port."""

    def ensure(
        self, repo_root: Path, workspace: Path, python: str | None = None
    ) -> TargetEnvironment:
        if python:
            supplied = Path(python).expanduser().resolve()
            if not supplied.is_file():
                raise EnvironmentNotReady(f"--python does not exist: {supplied}")
            return TargetEnvironment(supplied, "supplied")

        existing = discover(repo_root)
        if existing is not None:
            if has_pytest(existing):
                return TargetEnvironment(existing, "existing")
            log.info("%s has no pytest; provisioning a fresh environment instead", existing)

        return self._provision(repo_root, workspace)

    def _provision(self, repo_root: Path, workspace: Path) -> TargetEnvironment:
        venv_dir = workspace / "envs" / repo_root.name
        venv_dir.parent.mkdir(parents=True, exist_ok=True)

        log.info("creating virtualenv for %s at %s", repo_root.name, venv_dir)
        _run(["uv", "venv", str(venv_dir)], "create virtualenv")

        python = _venv_python(venv_dir)
        if python is None:
            raise EnvironmentNotReady(f"uv created {venv_dir} but no interpreter was found in it")

        for path in dependency_files(repo_root):
            _install(python, repo_root, path)

        if not has_pytest(python):
            log.info("target declares no pytest; installing it to run the suite")
            _run(["uv", "pip", "install", "--python", str(python), "pytest"], "install pytest")

        if not has_pytest(python):
            raise EnvironmentNotReady(f"pytest is still not runnable with {python}")

        return TargetEnvironment(python, "provisioned")


def _install(python: Path, repo_root: Path, path: Path) -> None:
    if path.name in ("pyproject.toml", "setup.py"):
        command = ["uv", "pip", "install", "--python", str(python), "-e", str(repo_root)]
    else:
        command = ["uv", "pip", "install", "--python", str(python), "-r", str(path)]

    log.info("installing dependencies from %s", path.name)
    result = _run(command, f"install {path.name}", check=False)
    if result.returncode != 0:
        # A repo whose install partially fails is still worth attempting: the
        # suite may only need the subset that resolved. Failing here would turn
        # a fixable run into no run at all.
        log.warning("installing %s failed (continuing):\n%s", path.name, result.stderr.strip())


def _run(command: list[str], what: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except FileNotFoundError as error:
        raise EnvironmentNotReady(
            f"could not {what}: `uv` is not on PATH (install from https://docs.astral.sh/uv/)"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise EnvironmentNotReady(
            f"could not {what}: timed out after {INSTALL_TIMEOUT}s"
        ) from error

    if check and result.returncode != 0:
        raise EnvironmentNotReady(f"could not {what}:\n{result.stderr.strip()}")
    return result
