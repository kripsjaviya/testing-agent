# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Greenfield. The only file present is `autonomous_bug_fixer_task.docx` (the assessment brief). No source, no
tests, no git repo yet. Everything below marked *(planned)* is a decision already made, not something to
re-litigate — but it does not exist on disk until written.

This folder is **deliberately not a git repo yet** — `git init` is a later step, not an oversight. The
final deliverable is a private GitHub repo.

## What this repo is

An **autonomous bug-fixing agent**: point it at a repository with a failing pytest suite, and it runs the
suite, localises each failure's root cause, patches it, re-verifies, reverts and retries on failure, and
loops until green or until it reports what it could not fix and why.

This is a 48-hour take-home assessment (role: Agentic LLM Developer). The graded weights are:
Agent Architecture 25%, Bug Detection & Fix Quality 25%, Error Recovery & Robustness 20%,
**Observability & Logging 15%**, Code Quality & Documentation 15%. Note that observability and docs are 30%
combined — logging and `DESIGN.md` are deliverables, not afterthoughts.

## Non-negotiable constraints (from the brief)

These are the rules the graders actively verify. Violating any one of them fails the submission:

- **No hard-coded bug knowledge.** The agent must not contain heuristics, prompts, patterns, or branches
  keyed to specific bugs, specific file names, or the reference app's domain. Graders run it against a
  *variant repo with different bugs*. If you find yourself writing `if "off_by_one" in ...` or a prompt that
  enumerates "look for missing await / SQL injection / unclosed connection", stop — that is the failure mode
  being tested for. Everything must be derived at runtime from test output, tracebacks, and source.
- **Target-repo agnostic.** The agent takes a repo path/URL as input. It must not assume FastAPI, SQLite,
  a particular layout, or a particular test count. Do not create synthetic fixture repos in this repo.
- **Cost ceiling: < $5 per full run**, with token usage tracked and reported.
- **Single command** to reproduce a full run.

## Confirmed stack decisions

| Decision | Choice | Why |
|---|---|---|
| Agent language | Python 3.10 (local: `C:\Program Files\Python310`) | Same runtime as target repos; stdlib `ast` for localisation; direct pytest/subprocess control |
| Orchestration | **LangGraph** | Explicit state-machine graph for the diagnose→patch→verify loop; checkpointing and conditional edges map onto retry/revert |
| LLM provider | **Anthropic (default) or Gemini**, user-selectable per run | One provider per run behind a common interface; cost-tier routing happens *within* the selected provider |
| Package manager | `uv` (available locally) | Fast, lockfile-backed |

**Provider selection.** The user picks the provider for a run (CLI flag / env var); **Anthropic is the
default**, Gemini is the alternative. Both sit behind one narrow interface — call sites request a *tier*,
never a vendor or a model name. Adding a third provider should mean adding one adapter plus a tier map,
touching nothing else. API keys are supplied by the user in a `.env` file (`ANTHROPIC_API_KEY` /
Gemini key); ship a committed `.env.example` and keep the real `.env` out of version control.

**Model routing (planned):** route by *task cost class*, not by bug type (bug type is unknown at runtime,
and keying on it would violate the no-hard-coding rule). Each provider defines a two-tier map:

- **cheap tier** — mechanical work: parsing pytest output, triage, summarising diffs
- **reasoning tier** — root-cause analysis and patch generation; also the escalation target after a failed
  patch attempt

All concrete model IDs live in **one config module** — never inline in call sites. Anthropic IDs available
here: `claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-5`. Verify current Gemini model IDs
before pinning them; do not guess. Cost accounting must stay correct across providers — per-token prices
differ, so the ledger records provider + model + token counts, not just tokens.

## Architecture (planned)

A LangGraph state machine over a shared run state (failing tests, per-bug attempt history, token/cost
ledger, patch stack). Node responsibilities:

- **Acquire** — resolve the target: a **local path** is used in place; a **GitHub URL** is cloned into a
  workspace directory first. Everything downstream sees a local path either way.
- **Discover** — locate and characterise the target repo's test setup (test runner invocation, deps,
  venv) without assuming a layout.
- **Run tests** — execute pytest, parse structured output (prefer machine-readable output over regex on
  human text), produce the failing-test set.
- **Localise** — for one failure: traceback frames → candidate files/symbols, narrowed with AST parsing and
  ripgrep-style search. Feed *ranked excerpts*, not whole-repo dumps — this is what makes it scale past the
  small reference app.
- **Patch** — generate a candidate fix as a **git-style diff**, not a full-file rewrite (bonus criterion).
- **Verify** — re-run the targeted test, then the full suite to catch regressions.
- **Decide** — on failure: revert, record why the hypothesis was wrong, retry with that context (bounded
  attempts), or give up on that bug and move on. Never leave a failed patch applied.

Bonus criteria worth wiring in from the start, because retrofitting is expensive: **confidence scoring**
(rate a patch before applying; apply high-confidence first), diff-based output, and cost routing.

## Rules for working in the target repo

- **Always snapshot before patching.** If the target is a git repo, use git to revert (`git diff` /
  `git checkout --`); if it is not, take a file snapshot first. Reverting cleanly is 20% of the grade.
- **Isolate the target's dependencies** from the agent's own — separate venv, invoked by absolute path.
- **Assume flakiness.** The reference suite is documented to include an async race condition that fails
  *intermittently*, and a resource leak that only shows up under sustained load. A single green run is not
  proof of a fix, and a single red run is not proof of a bug. Verification must distinguish "flaky" from
  "fixed" (repeat runs) and must not thrash on a test that fails nondeterministically.
- **Never mutate the target's tests** to make them pass. Fix source, not assertions.

## Observability requirements

Every LLM call, tool call, patch attempt, and verification result must be captured in a structured,
replayable trace — enough to audit *why* the agent believed a given root cause. A complete run trace is a
mandatory deliverable, and token/cost accounting must be per-call and aggregated.

## Mandatory deliverables

- Agent source, runnable with a single command
- `DESIGN.md` (1–2 pages): architecture, agent loop and control flow, tool design, retry/recovery strategy,
  LLM selection rationale, what you'd improve with more time
- Complete run log/trace of one successful run
- `README.md`: setup, required environment variables, the single reproduce command

## Commands

None yet — no code exists. When the entrypoint lands, document the real commands here (install, single full
run against a target repo, running the agent's own tests, and running one test in isolation).
