"""The coordinator: a LangGraph state machine over diagnose -> patch -> review ->
apply -> verify -> decide.

The graph is the coordinator in the multi-agent design. Each LLM role runs in its
own node with its own prompt and context; the graph owns routing, the attempt
budget, and -- critically -- the invariant that a failed patch is *always*
reverted before the next attempt begins.

Making this a graph rather than nested ifs buys one thing that matters: every
transition is named and traceable, so a run trace reads as the path taken through
a fixed set of states rather than as an opaque call stack.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..domain.localise import clear_caches
from ..domain.patching import build_patch
from ..domain.trace import Kind, Tracer
from ..ports import LLMError, SourceStore
from .agents import Diagnoser, Patcher, Reviewer
from .verify import Verifier

log = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_CONFIDENCE = 0.5


class FixState(TypedDict, total=False):
    # run context
    repo_root: str
    python: str
    timeout: int
    max_attempts: int
    min_confidence: float
    baseline_failures: list[str]

    # queue
    queue: list[str]
    failures: dict[str, Any]
    localisations: dict[str, Any]
    current: str

    # current attempt
    attempts: int
    history: list[str]
    hypothesis: str
    proposal: Any
    patch: Any
    review: Any
    verification: Any

    # results
    fixed: list[str]
    unfixed: list[dict]
    route: str


def build_graph(
    diagnoser: Diagnoser,
    patcher: Patcher,
    reviewer: Reviewer,
    verifier: Verifier,
    store: SourceStore,
    tracer: Tracer | None = None,
):
    """Wire the nodes into a compiled state machine."""

    def trace(kind: str, **data) -> None:
        if tracer is not None:
            tracer.emit(kind, **data)

    def give_up(state: FixState, reason: str) -> dict:
        """Record an unfixable failure and move on -- never abort the whole run."""
        entry = {
            "nodeid": state["current"],
            "reason": reason,
            "attempts": state.get("attempts", 0),
            "history": list(state.get("history", [])),
        }
        trace(Kind.DECISION, nodeid=state["current"], decision="give_up", reason=reason)
        return {
            "unfixed": state.get("unfixed", []) + [entry],
            "route": "next",
        }

    # --- nodes ------------------------------------------------------------

    def select_failure(state: FixState) -> dict:
        queue = list(state.get("queue", []))
        if not queue:
            return {"current": "", "route": "done"}

        nodeid = queue.pop(0)
        return {
            "queue": queue,
            "current": nodeid,
            "attempts": 0,
            "history": [],
            "hypothesis": "",
            "proposal": None,
            "patch": None,
            "review": None,
            "verification": None,
            "route": "work",
        }

    def diagnose(state: FixState) -> dict:
        nodeid = state["current"]
        try:
            hypothesis = diagnoser(
                state["failures"][nodeid],
                state["localisations"][nodeid],
                state.get("history", []),
            )
        except LLMError as error:
            return give_up(state, f"diagnosis failed: {error}")

        trace(Kind.DECISION, nodeid=nodeid, decision="diagnosed", hypothesis=hypothesis)
        return {
            "hypothesis": hypothesis,
            "attempts": state.get("attempts", 0) + 1,
            "route": "work",
        }

    def propose_patch(state: FixState) -> dict:
        nodeid = state["current"]
        localisation = state["localisations"][nodeid]
        try:
            proposal = patcher(
                state["failures"][nodeid],
                localisation,
                state["hypothesis"],
                state.get("history", []),
            )
        except LLMError as error:
            return give_up(state, f"patch generation failed: {error}")

        if proposal is None:
            return _retry(state, "the patcher returned no usable patch")

        # The model may only replace a symbol it was actually offered. This is
        # what stops a patch landing in a test file or somewhere unrelated --
        # it is enforced here rather than trusted to the prompt.
        excerpt = next(
            (
                e
                for e in localisation.excerpts
                if e.patchable and e.path == proposal.path and e.symbol == proposal.symbol
            ),
            None,
        )
        if excerpt is None:
            return _retry(
                state,
                f"proposed editing {proposal.path}::{proposal.symbol}, "
                "which is not an editable candidate",
            )

        path = Path(state["repo_root"]) / proposal.path
        try:
            patch = build_patch(proposal, store.read(path), excerpt.start_line, excerpt.end_line)
        except (OSError, ValueError) as error:
            return _retry(state, f"patch could not be built: {error}")

        if not patch.changes_anything:
            return _retry(state, "the proposed patch was identical to the current code")

        trace(
            Kind.PATCH_PROPOSED,
            nodeid=nodeid,
            path=proposal.path,
            symbol=proposal.symbol,
            rationale=proposal.rationale,
            diff=patch.diff,
        )
        return {"proposal": proposal, "patch": patch, "route": "work"}

    def review_patch(state: FixState) -> dict:
        nodeid = state["current"]
        try:
            review = reviewer(state["failures"][nodeid], state["hypothesis"], state["patch"])
        except LLMError as error:
            return give_up(state, f"review failed: {error}")

        threshold = state.get("min_confidence", DEFAULT_MIN_CONFIDENCE)
        accepted = review.apply and review.confidence >= threshold
        trace(
            Kind.DECISION,
            nodeid=nodeid,
            decision="review",
            confidence=review.confidence,
            accepted=accepted,
            reason=review.reason,
        )

        if not accepted:
            return _retry(
                state,
                f"reviewer rejected the patch (confidence {review.confidence:.2f}): "
                f"{review.reason or 'no reason given'}",
                review=review,
            )
        return {"review": review, "route": "work"}

    def apply_patch(state: FixState) -> dict:
        patch = state["patch"]
        path = Path(state["repo_root"]) / patch.proposal.path
        store.write(path, patch.after)
        # Ranking must not be done against the pre-patch AST on a later attempt.
        clear_caches()
        trace(
            Kind.PATCH_APPLIED,
            nodeid=state["current"],
            path=patch.proposal.path,
            symbol=patch.proposal.symbol,
        )
        return {"route": "work"}

    def verify_patch(state: FixState) -> dict:
        verification = verifier(
            Path(state["repo_root"]),
            state["python"],
            state["current"],
            set(state.get("baseline_failures", [])),
            timeout=state.get("timeout", 600),
        )
        return {"verification": verification, "route": "work"}

    def decide(state: FixState) -> dict:
        nodeid = state["current"]
        verification = state["verification"]

        if verification.fixed:
            store.commit()
            trace(
                Kind.DECISION,
                nodeid=nodeid,
                decision="accepted",
                summary=verification.summary(),
            )

            # The regression run is a full-suite measurement of the patched tree,
            # so it also says which *other* queued failures this patch cleared.
            # Failing tests are not one-to-one with bugs, and re-diagnosing a test
            # that now passes would waste a whole diagnose/patch/review cycle.
            queue = list(state.get("queue", []))
            collateral: list[str] = []
            if verification.suite is not None:
                still_failing = set(verification.suite.failing_ids)
                collateral = [n for n in queue if n not in still_failing]
                queue = [n for n in queue if n in still_failing]
                if collateral:
                    trace(
                        Kind.DECISION,
                        nodeid=nodeid,
                        decision="cleared_others",
                        also_fixed=collateral,
                    )

            return {
                "queue": queue,
                "fixed": state.get("fixed", []) + [nodeid] + collateral,
                "route": "next",
            }

        # Anything short of fixed is reverted. A patch that half-works is still
        # a patch the next attempt would have to reason around.
        store.rollback()
        clear_caches()
        trace(
            Kind.PATCH_REVERTED,
            nodeid=nodeid,
            reason=verification.summary(),
        )
        return _retry(state, verification.summary())

    def _retry(state: FixState, reason: str, **extra) -> dict:
        """Record why this attempt failed and route to another try, or give up."""
        history = state.get("history", []) + [reason]
        attempts = state.get("attempts", 0)
        limit = state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)

        if attempts >= limit:
            trace(
                Kind.DECISION,
                nodeid=state["current"],
                decision="give_up",
                reason=f"{attempts} attempt(s) exhausted; last: {reason}",
            )
            entry = {
                "nodeid": state["current"],
                "reason": reason,
                "attempts": attempts,
                "history": history,
            }
            return {
                "history": history,
                "unfixed": state.get("unfixed", []) + [entry],
                "route": "next",
                **extra,
            }

        trace(Kind.DECISION, nodeid=state["current"], decision="retry", reason=reason)
        return {"history": history, "route": "retry", **extra}

    # --- wiring -----------------------------------------------------------

    graph = StateGraph(FixState)
    graph.add_node("select", select_failure)
    graph.add_node("diagnose", diagnose)
    graph.add_node("propose", propose_patch)
    graph.add_node("review", review_patch)
    graph.add_node("apply", apply_patch)
    graph.add_node("verify", verify_patch)
    graph.add_node("decide", decide)

    graph.add_edge(START, "select")
    graph.add_conditional_edges(
        "select",
        lambda s: s.get("route", "done"),
        {"work": "diagnose", "done": END},
    )
    graph.add_conditional_edges(
        "diagnose",
        lambda s: s.get("route", "work"),
        {"work": "propose", "next": "select", "retry": "diagnose"},
    )
    graph.add_conditional_edges(
        "propose",
        lambda s: s.get("route", "work"),
        {"work": "review", "retry": "diagnose", "next": "select"},
    )
    graph.add_conditional_edges(
        "review",
        lambda s: s.get("route", "work"),
        {"work": "apply", "retry": "diagnose", "next": "select"},
    )
    graph.add_edge("apply", "verify")
    graph.add_edge("verify", "decide")
    graph.add_conditional_edges(
        "decide",
        lambda s: s.get("route", "next"),
        {"retry": "diagnose", "next": "select"},
    )

    return graph.compile()
