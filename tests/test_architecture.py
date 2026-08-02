"""Executable dependency rules.

A layering convention that is only written down erodes. These tests read the
import graph and fail the build when a layer reaches somewhere it must not, so
the architecture is enforced rather than merely documented.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "bugfixer"

THIRD_PARTY = {"anthropic", "google", "dotenv", "langgraph", "pytest"}

# layer -> package names it may NOT import
FORBIDDEN = {
    "domain": {"adapters", "application", "config", "ports", "bootstrap", "cli"} | THIRD_PARTY,
    "ports": {"adapters", "application", "config", "bootstrap", "cli"} | THIRD_PARTY,
    "application": {"adapters", "bootstrap", "cli"},
    "adapters": {"application", "bootstrap", "cli"},
}


def modules_in(layer: str) -> list[Path]:
    return sorted(p for p in (SRC / layer).rglob("*.py") if p.name != "__init__.py")


def imports_of(path: Path) -> set[str]:
    """Top-level names this module imports, with relative imports resolved to layers."""
    # utf-8-sig for the same reason the agent uses it: a BOM-prefixed source
    # would raise SyntaxError and silently exempt that file from these rules.
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from ..adapters.x import y` -> "adapters"; `from .models` -> ""
                names.add((node.module or "").split(".")[0])
            elif node.module:
                parts = node.module.split(".")
                names.add(parts[1] if parts[0] == "bugfixer" and len(parts) > 1 else parts[0])
    return names - {""}


def cases(layer: str):
    return [pytest.param(p, id=str(p.relative_to(SRC)).replace("\\", "/")) for p in modules_in(layer)]


@pytest.mark.parametrize("module", cases("domain"))
def test_domain_is_pure(module):
    """Domain may use the standard library and other domain modules -- nothing else."""
    leaked = imports_of(module) & FORBIDDEN["domain"]
    assert not leaked, f"{module.name} imports {sorted(leaked)}; domain must stay pure"


@pytest.mark.parametrize("module", cases("application"))
def test_application_never_imports_an_adapter(module):
    leaked = imports_of(module) & FORBIDDEN["application"]
    assert not leaked, f"{module.name} imports {sorted(leaked)}; use cases depend on ports only"


@pytest.mark.parametrize("module", cases("adapters"))
def test_adapters_never_import_a_use_case(module):
    leaked = imports_of(module) & FORBIDDEN["adapters"]
    assert not leaked, f"{module.name} imports {sorted(leaked)}; adapters sit below the use cases"


def test_ports_depend_only_on_domain():
    leaked = imports_of(SRC / "ports.py") & FORBIDDEN["ports"]
    assert not leaked, f"ports.py imports {sorted(leaked)}"


def test_only_the_composition_root_constructs_adapters():
    """Adapters are wired in exactly one place, so swapping one is a single edit."""
    offenders = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative in {"bootstrap.py", "cli.py"} or relative.startswith("adapters/"):
            continue
        if "adapters" in imports_of(path):
            offenders.append(relative)

    assert not offenders, f"adapters constructed outside the composition root: {offenders}"


def test_every_layer_has_modules():
    """Guards the tests above against silently passing on an empty glob."""
    for layer in ("domain", "application", "adapters"):
        assert modules_in(layer), f"no modules found in {layer}"
