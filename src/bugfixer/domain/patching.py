"""Turning a model's answer into an applicable change, and into a diff.

The model returns a corrected *symbol*, which is spliced in by AST line range;
the unified diff is then computed with difflib. Asking a model to emit a diff
directly is the more literal reading of "diff-based output", but LLM-authored
hunks routinely carry wrong offsets and fail to apply. This yields a reliable
apply and a real git-style diff.

Everything here is pure: text in, text out. No file is read or written.
"""

from __future__ import annotations

import difflib
import re
import textwrap
from dataclasses import dataclass

FENCE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)
_FIELD = r"^\s*{name}\s*:\s*(?P<value>.+?)\s*$"


@dataclass(frozen=True)
class PatchProposal:
    """A model's suggested replacement for one symbol."""

    path: str  # relative to the repo root
    symbol: str
    rationale: str
    new_source: str


@dataclass(frozen=True)
class Patch:
    """A proposal resolved against the real file, with its diff."""

    proposal: PatchProposal
    before: str
    after: str
    diff: str

    @property
    def changes_anything(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class Review:
    confidence: float
    apply: bool
    reason: str


def extract_code(text: str) -> str:
    """Pull the code out of a fenced block, tolerating an unfenced answer."""
    matches = FENCE.findall(text)
    if matches:
        # The last block is the answer when a model shows the original first.
        return matches[-1].strip("\n")
    return text.strip()


def _field(text: str, name: str, default: str = "") -> str:
    match = re.search(_FIELD.format(name=name), text, re.MULTILINE | re.IGNORECASE)
    return match.group("value").strip() if match else default


def parse_proposal(text: str) -> PatchProposal | None:
    """Parse the patcher's reply. Returns None when it is unusable."""
    code = extract_code(text)
    if not code.strip():
        return None

    path = _field(text, "FILE")
    symbol = _field(text, "SYMBOL")
    if not path or not symbol:
        return None

    return PatchProposal(
        path=path,
        symbol=symbol,
        rationale=_field(text, "RATIONALE"),
        new_source=code,
    )


def parse_review(text: str) -> Review:
    """Parse the reviewer's reply, defaulting to caution when it is unclear."""
    raw = _field(text, "CONFIDENCE", "0")
    try:
        confidence = float(re.sub(r"[^0-9.]", "", raw) or 0)
    except ValueError:
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    verdict = _field(text, "VERDICT").lower()
    reason = _field(text, "REASON")
    # An unparseable verdict must not read as approval.
    return Review(confidence=confidence, apply=verdict.startswith("apply"), reason=reason)


def detect_newline(source: str) -> str:
    return "\r\n" if "\r\n" in source else "\n"


def replace_symbol(source: str, start_line: int, end_line: int, replacement: str) -> str:
    """Splice ``replacement`` over lines ``start_line..end_line`` (1-indexed).

    The replacement is re-indented to the original symbol's column, so a method
    returned at column 0 still lands correctly inside its class. Line endings are
    preserved -- a patch must not rewrite every line of a CRLF file.
    """
    newline = detect_newline(source)
    lines = source.split(newline)

    if start_line < 1 or end_line > len(lines) or start_line > end_line:
        raise ValueError(
            f"symbol lines {start_line}-{end_line} are outside a {len(lines)}-line file"
        )

    original_indent = _leading_whitespace(lines[start_line - 1])
    body = textwrap.indent(textwrap.dedent(replacement).rstrip("\n"), original_indent)

    patched = lines[: start_line - 1] + body.split("\n") + lines[end_line:]
    return newline.join(patched)


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def render_diff(path: str, before: str, after: str) -> str:
    """A git-style unified diff."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff)


def build_patch(
    proposal: PatchProposal, file_text: str, start_line: int, end_line: int
) -> Patch:
    after = replace_symbol(file_text, start_line, end_line, proposal.new_source)
    return Patch(
        proposal=proposal,
        before=file_text,
        after=after,
        diff=render_diff(proposal.path, file_text, after),
    )
