"""The three LLM roles: diagnose, patch, review.

Each has its own system prompt and its own context window; none sees the others'
instructions. The coordinator (the graph) routes between them.

**No prompt here names a kind of bug.** They describe the *task* -- read this
failure, read this code, explain the cause -- and never a catalogue of things to
look for. Enumerating bug types would hand the agent the answer for one repo and
make it worse on any other, which is precisely what is being tested for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.models import CaseReport, Localisation, Tier
from ..domain.patching import Patch, PatchProposal, Review, parse_proposal, parse_review
from .router import LLMRouter

log = logging.getLogger(__name__)

DIAGNOSER_SYSTEM = """\
You are a debugging specialist working on an unfamiliar Python codebase.

You are given one failing test, its exact error output, and excerpts of source
code ranked by how likely each is to contain the cause. Identify the single most
likely root cause of this specific failure.

Rules:
- Reason only from the evidence given. Do not guess at code you cannot see.
- The test is correct. Explain why the source produces the observed result.
- Do not propose a patch. That is a separate step.
- If the excerpts are insufficient to reach a confident conclusion, say so
  plainly and state what would be needed.

Answer in under 150 words."""

PATCHER_SYSTEM = """\
You are a code repair specialist working on an unfamiliar Python codebase.

Given a failing test, a root-cause analysis, and the relevant source, return a
corrected version of exactly ONE symbol (one function, method, or class).

Rules:
- Change the minimum necessary to correct the cause. Do not refactor, rename,
  reformat, or add features.
- Never modify a test file. Fix the source so the existing test passes.
- Return the complete symbol, not a fragment or a diff.
- Choose a symbol from the candidates you were shown.

Reply in exactly this format:

FILE: <path exactly as shown in the candidates>
SYMBOL: <name of the symbol you are replacing>
RATIONALE: <one sentence on what was wrong>
```python
<the complete corrected symbol>
```"""

REVIEWER_SYSTEM = """\
You are reviewing a proposed one-symbol patch before it is applied.

You are given the failing test, its error output, the stated root cause, and the
patch as a unified diff. Judge whether the change plausibly makes that test pass
without breaking behaviour that other code depends on.

Weigh against it: changes wider than the stated cause, altered signatures that
callers rely on, hard-coding a value to satisfy the assertion, or a diff that does
not follow from the analysis.

Reply in exactly this format:

CONFIDENCE: <number between 0.0 and 1.0>
VERDICT: <apply or reject>
REASON: <one sentence>"""


def _failure_block(failure: CaseReport) -> str:
    return (
        f"Failing test: {failure.nodeid}\n"
        f"Failed during: {failure.phase}\n"
        f"Error: {failure.message.strip()}\n\n"
        f"Traceback:\n{failure.longrepr.strip()}"
    )


def _history_block(history: list[str]) -> str:
    if not history:
        return ""
    attempts = "\n".join(f"{i}. {entry}" for i, entry in enumerate(history, start=1))
    return (
        "\n\nPrevious attempts on this failure did not work. Do not repeat them:\n"
        f"{attempts}"
    )


@dataclass
class Diagnoser:
    """Explains why a test fails. Reasoning tier: this determines correctness."""

    router: LLMRouter

    def __call__(
        self, failure: CaseReport, localisation: Localisation, history: list[str]
    ) -> str:
        prompt = (
            f"{_failure_block(failure)}\n\n"
            f"Candidate source, most likely first:\n\n{localisation.render()}"
            f"{_history_block(history)}"
        )
        response = self.router.complete(
            Tier.REASONING, DIAGNOSER_SYSTEM, prompt, label="diagnose"
        )
        return response.text.strip()


@dataclass
class Patcher:
    """Produces a corrected symbol. Reasoning tier."""

    router: LLMRouter

    def __call__(
        self,
        failure: CaseReport,
        localisation: Localisation,
        hypothesis: str,
        history: list[str],
    ) -> PatchProposal | None:
        editable = [e for e in localisation.excerpts if e.patchable]
        listing = "\n".join(f"- {e.path} :: {e.symbol}" for e in editable)

        prompt = (
            f"{_failure_block(failure)}\n\n"
            f"Root-cause analysis:\n{hypothesis}\n\n"
            f"Source:\n\n{localisation.render()}\n\n"
            f"You may replace exactly one of these symbols:\n{listing}"
            f"{_history_block(history)}"
        )
        response = self.router.complete(
            Tier.REASONING, PATCHER_SYSTEM, prompt, label="patch"
        )
        return parse_proposal(response.text)


@dataclass
class Reviewer:
    """Rates a patch before it is applied. Cheap tier: this is a judgement call
    on material already produced, not fresh analysis."""

    router: LLMRouter

    def __call__(self, failure: CaseReport, hypothesis: str, patch: Patch) -> Review:
        prompt = (
            f"{_failure_block(failure)}\n\n"
            f"Stated root cause:\n{hypothesis}\n\n"
            f"Proposed patch:\n```diff\n{patch.diff}\n```"
        )
        response = self.router.complete(
            Tier.CHEAP, REVIEWER_SYSTEM, prompt, label="review"
        )
        return parse_review(response.text)
