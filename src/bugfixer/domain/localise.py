"""Rank candidate code for one test failure.

The agent must never send a whole repository to a model, so this module turns a
failure into a short, ranked list of *coherent* code units -- whole functions or
classes pulled with the AST, not arbitrary line windows.

Two things drive the design:

*Assertion failures produce no source frames.* When ``assert double(5) == 10``
fails, the traceback names only the test file. Since the agent must never patch
tests, a ranker that trusted frames alone would return zero patchable candidates
for exactly the class of bug that shows up as a wrong return value. So the
failing test's own body is parsed and the functions it calls are resolved against
a repo-wide symbol index. That expansion is the primary path, not a fallback.

*Nothing here may encode bug knowledge.* Every signal -- frame depth, whether a
path is a test, which identifiers matter -- is derived at runtime from the test
output and the source. The stop-word set is Python's own keywords and builtins,
not a hand-written list of interesting terms.
"""

from __future__ import annotations

import ast
import builtins
import keyword
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .models import CaseReport, Excerpt, Localisation, Symbol

log = logging.getLogger(__name__)

# Directories that are never patch candidates: not ours to change, or not code.
EXCLUDED_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "env", "__pycache__", "site-packages",
        "node_modules", "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(keyword.kwlist) | frozenset(dir(builtins))

MAX_EXCERPTS = 8
MAX_EXCERPT_LINES = 120

# Scores are relative, not absolute -- only the ordering matters. Source frames
# outrank everything because the interpreter pointed straight at them; callees
# outrank the test body because the test is context we may read but never edit.
SCORE_SOURCE_FRAME = 10.0
SCORE_CALLEE = 6.0
# A call in the assert expression is the subject of the test; a call earlier in
# the body is usually scaffolding (fixtures, clients, builders). Without this
# split, `AlphaHandler()` on the setup line ties with the function under test.
SCORE_CALLEE_SETUP = 3.0
SCORE_TEST_FRAME = 2.0
SCORE_DEPTH_BONUS = 2.0
SCORE_IDENTIFIER_MATCH = 1.5
MAX_IDENTIFIER_BONUS = 4.5

# A name defined in many places is weak evidence: a test calling `client.get()`
# should not nominate every `get` in the repo. Above this many definitions the
# name says nothing useful and is dropped rather than diluted.
MAX_CALLEE_MATCHES = 8


def is_test_path(path: Path) -> bool:
    """Pytest's own discovery convention -- a property of the runner, not of any bug."""
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in {part.lower() for part in path.parts}
        or name == "conftest.py"
    )


def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if EXCLUDED_DIRS.isdisjoint(path.parts):
            yield path


# utf-8-sig, not utf-8: a BOM-prefixed source file is common on Windows and
# leaves a leading ï»¿ that makes ast.parse raise SyntaxError, which would
# silently drop every symbol in the file. Reading as utf-8-sig strips a BOM when
# present and is identical to utf-8 when it is not.
SOURCE_ENCODING = "utf-8-sig"


@lru_cache(maxsize=512)
def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(
            path.read_text(encoding=SOURCE_ENCODING, errors="replace"), filename=str(path)
        )
    except (SyntaxError, OSError) as error:
        # A repo with a syntax error is exactly the situation the agent exists to
        # fix; skip the file for ranking rather than aborting the run.
        log.debug("could not parse %s: %s", path, error)
        return None


def clear_caches() -> None:
    """Drop parsed ASTs.

    Must be called after the agent edits the target, or a re-localisation would
    rank against the pre-patch source and report stale line numbers.
    """
    _parse.cache_clear()


def _definitions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node


def enclosing_symbol(path: Path, lineno: int | None) -> Symbol | None:
    """Innermost def/class containing ``lineno``, so excerpts are whole units."""
    tree = _parse(path)
    if tree is None or lineno is None:
        return None

    best = None
    for node in _definitions(tree):
        end = getattr(node, "end_lineno", None) or node.lineno
        if node.lineno <= lineno <= end:
            # Innermost wins: a method beats the class that contains it.
            if best is None or node.lineno > best.lineno:
                best = node

    if best is None:
        return None
    return Symbol(path, best.name, best.lineno, getattr(best, "end_lineno", best.lineno))


def called_names(path: Path, symbol: Symbol) -> set[str]:
    """Names invoked inside ``symbol`` -- the functions a failing test exercises."""
    tree = _parse(path)
    if tree is None:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (symbol.start_line <= node.lineno <= symbol.end_line):
            continue

        names |= _call_name(node)
    return names - _STOPWORDS


def _call_name(node: ast.Call) -> set[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return {func.id}
    if isinstance(func, ast.Attribute):
        return {func.attr}
    return set()


def asserted_names(path: Path, symbol: Symbol) -> set[str]:
    """Names called inside ``symbol``'s assertions -- what the test is really about.

    Also covers ``with pytest.raises(...)`` blocks, where the subject is the call
    in the body rather than an assert expression. Both are pytest conventions,
    not assumptions about any particular bug.
    """
    tree = _parse(path)
    if tree is None:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not (symbol.start_line <= getattr(node, "lineno", -1) <= symbol.end_line):
            continue

        subject = None
        if isinstance(node, ast.Assert):
            subject = node
        elif isinstance(node, ast.With):
            if any(
                isinstance(item.context_expr, ast.Call)
                and "raises" in _call_name(item.context_expr)
                for item in node.items
            ):
                subject = node

        if subject is not None:
            for inner in ast.walk(subject):
                if isinstance(inner, ast.Call):
                    names |= _call_name(inner)
    return names - _STOPWORDS


def imported_modules(path: Path) -> dict[str, str]:
    """Local name -> module it was imported from.

    A failing test almost always names its subject directly (``from calc import
    double``), which resolves a callee to one file exactly. Matching on the bare
    name is the fallback for everything else.
    """
    tree = _parse(path)
    if tree is None:
        return {}

    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and not node.level:  # skip relative imports
                for alias in node.names:
                    mapping[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    mapping[alias.asname] = alias.name
                else:
                    # `import a.b` binds `a`, not `a.b` -- so the name refers to
                    # the top package. Recording a.b here would let a later
                    # submodule import clobber the binding for `a`.
                    root_name = alias.name.split(".")[0]
                    mapping.setdefault(root_name, root_name)
    return mapping


def module_path(module: str, root: Path) -> Path | None:
    """Resolve a dotted module name to a file inside the target repo."""
    parts = module.split(".")
    for candidate in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")):
        if candidate.is_file():
            return candidate.resolve()
    return None


class SymbolIndex:
    """Repo-wide name -> definition map, so a callee can be found in any file."""

    def __init__(self, root: Path):
        self.root = root
        self._by_name: dict[str, list[Symbol]] = {}
        self._built = False

    def _build(self) -> None:
        for path in iter_python_files(self.root):
            tree = _parse(path)
            if tree is None:
                continue
            for node in _definitions(tree):
                symbol = Symbol(
                    path=path,
                    name=node.name,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
                self._by_name.setdefault(node.name, []).append(symbol)
        self._built = True
        log.debug("indexed %d symbol name(s) under %s", len(self._by_name), self.root)

    def lookup(self, name: str) -> list[Symbol]:
        if not self._built:
            self._build()
        return self._by_name.get(name, [])


def failure_identifiers(failure: CaseReport) -> set[str]:
    """Identifier-shaped tokens from the failure text, minus Python's own vocabulary."""
    text = f"{failure.message}\n{failure.longrepr}"
    return {match.group(0) for match in _IDENTIFIER.finditer(text)} - _STOPWORDS


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding=SOURCE_ENCODING, errors="replace").splitlines()
    except OSError:
        return []


def _excerpt_source(symbol: Symbol) -> str:
    lines = _read_lines(symbol.path)
    if not lines:
        return ""

    body = lines[symbol.start_line - 1 : symbol.end_line]
    if len(body) > MAX_EXCERPT_LINES:
        kept = body[:MAX_EXCERPT_LINES]
        kept.append(f"    # ... {len(body) - MAX_EXCERPT_LINES} more line(s) truncated")
        body = kept
    return "\n".join(body)


def _in_repo(path: Path, root: Path) -> bool:
    return path.is_relative_to(root) and EXCLUDED_DIRS.isdisjoint(path.relative_to(root).parts)


def _resolve(raw: str, root: Path) -> Path | None:
    """Map a traceback frame's path onto a file inside the target repo."""
    if not raw:
        return None

    path = Path(raw)
    direct = path if path.is_absolute() else root / path
    try:
        direct = direct.resolve()
    except OSError:
        direct = None
    if direct is not None and direct.is_file() and _in_repo(direct, root):
        return direct

    return _relocated(path, root)


def _relocated(path: Path, root: Path) -> Path | None:
    """Find a frame's file inside the repo when its recorded path is elsewhere.

    A checkout that was copied or moved carries stale ``__pycache__`` bytecode,
    and a ``.pyc`` embeds the absolute path of the file it was compiled from. The
    interpreter then reports tracebacks against a directory that may not exist,
    which would leave the ranker with nothing to work on for the entire run.

    The longest trailing portion of the recorded path that exists inside the repo
    wins. At least two components must match, so a bare filename cannot pull in an
    unrelated file that happens to share a name.
    """
    parts = path.parts
    for start in range(1, len(parts) - 1):
        candidate = root.joinpath(*parts[start:])
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate.is_file() and _in_repo(candidate, root):
            log.debug("frame %s relocated to %s", path, candidate)
            return candidate
    return None


@dataclass
class _Candidate:
    symbol: Symbol
    score: float
    patchable: bool
    reasons: list[str] = field(default_factory=list)


def localise(
    root: Path,
    failure: CaseReport,
    index: SymbolIndex | None = None,
    max_excerpts: int = MAX_EXCERPTS,
) -> Localisation:
    """Rank code worth showing a model for ``failure``."""
    root = root.resolve()
    index = index or SymbolIndex(root)
    identifiers = failure_identifiers(failure)

    candidates: dict[tuple[Path, str, int], _Candidate] = {}

    def add(symbol: Symbol, score: float, patchable: bool, reason: str) -> None:
        key = (symbol.path, symbol.name, symbol.start_line)
        existing = candidates.get(key)
        if existing is None:
            candidates[key] = _Candidate(symbol, score, patchable, [reason])
        elif score > existing.score:
            # Keep the strongest justification's score, but retain every reason
            # so the trace shows all the evidence that pointed here.
            existing.score = score
            existing.reasons.append(reason)
        else:
            existing.reasons.append(reason)

    frames = failure.frames
    for position, frame in enumerate(frames):
        path = _resolve(frame.path, root)
        if path is None:
            continue  # stdlib, site-packages, or a file outside the target repo

        symbol = enclosing_symbol(path, frame.lineno)
        if symbol is None:
            continue

        # Later frames are nearer the raise, so they get more of the bonus.
        depth = (position + 1) / len(frames)
        relative = path.relative_to(root).as_posix()

        if is_test_path(path):
            add(symbol, SCORE_TEST_FRAME, False, f"traceback frame in test {relative}")
            # The reason source frames may be absent entirely: an assertion
            # failure never leaves the test. Expand into what the test called.
            imports = imported_modules(path)
            asserted = asserted_names(path, symbol)
            for name in called_names(path, symbol):
                matches = [c for c in index.lookup(name) if not is_test_path(c.path)]
                if not matches:
                    continue

                under_assertion = name in asserted
                base = SCORE_CALLEE if under_assertion else SCORE_CALLEE_SETUP
                role = "asserts on" if under_assertion else "calls"

                origin = imports.get(name)
                declared = module_path(origin, root) if origin else None
                from_import = [c for c in matches if c.path == declared] if declared else []

                if from_import:
                    # The test's own import statement pins this name to one file.
                    for callee in from_import:
                        add(
                            callee,
                            base,
                            True,
                            f"{symbol.name} {role} {name}(), imported from {origin}",
                        )
                    continue

                if len(matches) > MAX_CALLEE_MATCHES:
                    log.debug("skipping %s: %d definitions, too ambiguous", name, len(matches))
                    continue

                # Unimported name: still a lead, but weaker the more places it
                # is defined, so common method names cannot crowd out real hits.
                for callee in matches:
                    add(
                        callee,
                        base / len(matches),
                        True,
                        f"{symbol.name} {role} {name}() ({len(matches)} candidate definitions)",
                    )
        else:
            add(
                symbol,
                SCORE_SOURCE_FRAME + depth * SCORE_DEPTH_BONUS,
                True,
                f"traceback frame in {relative}:{frame.lineno}",
            )

    for candidate in candidates.values():
        matched = identifiers & _symbol_identifiers(candidate.symbol)
        if matched:
            bonus = min(len(matched) * SCORE_IDENTIFIER_MATCH, MAX_IDENTIFIER_BONUS)
            candidate.score += bonus
            candidate.reasons.append(
                "failure text mentions " + ", ".join(sorted(matched)[:4])
            )

    ranked = sorted(candidates.values(), key=lambda c: (-c.score, str(c.symbol.path)))
    excerpts = tuple(
        Excerpt(
            path=c.symbol.path.relative_to(root).as_posix(),
            symbol=c.symbol.name,
            start_line=c.symbol.start_line,
            end_line=c.symbol.end_line,
            source=_excerpt_source(c.symbol),
            patchable=c.patchable,
            score=round(c.score, 2),
            reasons=tuple(c.reasons),
        )
        for c in ranked[:max_excerpts]
    )
    return Localisation(nodeid=failure.nodeid, excerpts=excerpts)


def _symbol_identifiers(symbol: Symbol) -> set[str]:
    source = _excerpt_source(symbol)
    return {m.group(0) for m in _IDENTIFIER.finditer(source)} - _STOPWORDS
