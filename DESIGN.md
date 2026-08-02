# DESIGN

An autonomous agent that diagnoses and fixes bugs in a repository with a failing
pytest suite. This document covers the architecture, the agent loop, tool design,
retry and recovery, LLM selection, and what I would do with more time.

## 1. Architecture

Hexagonal, with the dependency rule enforced by a test rather than a convention.

```
          cli.py  ──────────────►  application/  ──────────►  domain/
      (translation only)            (use cases)             (pure logic)
                │                        │
                │                        ▼
                │                     ports.py
                │              (Protocols over value objects)
                ▼                        ▲
          bootstrap.py  ─────────►  adapters/
      (composition root)      anthropic · gemini · pytest
                              uv · filesystem · jsonl · fakes
```

| Layer | Contents | May depend on |
|---|---|---|
| `domain/` | `models` (value objects), `localise`, `cost`, `trace` | standard library only |
| `ports.py` | `LLMProvider`, `SuiteRunner`, `RepoAcquirer`, `EnvironmentProvider`, `SourceStore`, `TraceSink` | domain |
| `application/` | `diagnose`, `router` | domain, ports |
| `adapters/` | one module per technology, plus `fakes` | domain, ports |
| `bootstrap.py` | the only place adapters are constructed | everything |

**Why this shape.** Two properties fall out of it that the task specifically
rewards. First, every I/O boundary has an in-memory twin, so retry, revert, and
flaky-test handling are testable deterministically — a flaky test is
`FakeSuiteRunner(failing, passing, passing)` rather than a real race condition
someone has to reproduce. Second, "works against a variant repo with different
bugs" is structural: nothing above `adapters/` knows what a FastAPI app or a
SQLite connection is.

`tests/test_architecture.py` parses the import graph and fails the build if a
layer reaches somewhere it must not. A layering rule that is only written down
erodes; this one cannot.

### Value objects, not entities

`CaseReport`, `Excerpt`, `Localisation` carry facts out of pytest and the source
tree. They have no identity and no lifecycle, so they hold only derived behaviour
(`failed`, `patch_targets`, `render`) and the real work lives in domain services.
Aggregates, repositories, and domain events were considered and rejected: there is
no persistence and no request lifecycle here to justify them.

## 2. Agent loop and control flow

```
acquire ─► provision env ─► run suite ─► localise each failure
                                              │
                                              ▼
                                     ┌── propose patch ◄────────┐
                                     │        │                 │
                                     │        ▼                 │
                                     │  apply (snapshot)        │
                                     │        │                 │
                                     │        ▼                 │
                                     │   verify ×3 targeted     │
                                     │        │                 │
                                     │   ┌────┴────┐            │
                                     │ pass      fail           │
                                     │   │         │            │
                                     │   ▼         ▼            │
                                     │ full     revert ─► retry with
                                     │ suite               failure context
                                     │   │                  (bounded)
                                     │   ├── regression ─► revert ──┘
                                     │   ▼
                                     └─ commit, move to next failure
```

**Exit conditions.** Suite green; or every remaining failure has exhausted its
attempt budget; or the cost ledger hits the run budget. Each produces a report of
what could not be fixed and why, rather than silence.

**Ordering and pruning.** Failures are attempted strongest-localisation first,
scored by the top candidate the ranker produced. After a patch is accepted, its
regression run is a full-suite measurement of the patched tree — so it also says
which *other* queued failures that patch cleared. Those are removed from the
queue and reported as fixed without ever reaching a model.

This matters more than it sounds: failing tests are not one-to-one with bugs. In
the sample project one SQL-injection bug fails two tests, and pruning turns that
into one diagnose/patch/review cycle instead of two.

**Scoping.** `--only` and `--max-failures` bound a run, so a large or slow
repository can be tried a bite at a time rather than all at once.

## 3. Tool design

Four decisions where the obvious approach was the wrong one.

**Structured test results, not parsed terminal output.** A pytest plugin is
injected with `-p` plus a `PYTHONPATH` entry pointing at a directory containing
only that plugin. The target gains no dependency and none of its files change.
Regexes over terminal text break on any target that customises reporting, and —
more importantly — text gives no traceback *frame chain*, which is what ranking
depends on.

**Localisation ranks; it does not dump.** Feeding a repo to a model does not scale
and buries the signal. The ranker turns one failure into a short list of whole AST
symbols. The load-bearing detail: **an assertion failure produces no source
frames.** When `assert double(5) == 10` fails, the traceback names only the test
file, and tests must never be patched — so a frame-only ranker returns *zero*
patchable candidates for exactly the bugs that surface as a wrong return value.
The failing test's body is therefore parsed and its callees resolved through its
own `import` statements against a repo-wide symbol index.

| Signal | Score | Rationale |
|---|---|---|
| Traceback frame in source | 10.0 + depth | The interpreter pointed at it |
| Callee under the failing `assert` | 6.0 | The subject of the test |
| Callee in setup lines | 3.0 | Scaffolding, not the subject |
| Frame in a test file | 2.0 | Context; never patchable |
| Identifier overlap with failure text | +1.5 each, capped | Runtime evidence |

Names defined in many places are divided by their match count and dropped past a
threshold — a test calling `client.get()` must not nominate every `get` in the
repo. Every candidate carries the reasons it ranked, which is the audit trail.

**Patches replace symbols; diffs are computed.** The model returns a corrected
function body, which is spliced in by AST line range, and the unified diff is
generated with `difflib`. Asking a model to emit a diff directly is the more
literal reading of "diff-based output", but LLM-authored hunks routinely carry
wrong offsets. This gets a reliable apply *and* a real git-style diff.

**Rollback snapshots only what is touched.** Not `git checkout -- .`: the target
may not be a git repo, and if it is, it may hold uncommitted work belonging to the
user. Only files the agent writes are captured, so a revert can never destroy
work the agent did not do.

## 4. Retry and recovery

| Signal | Response |
|---|---|
| Patch fails its targeted test | Revert, retry with the failure text as evidence the hypothesis was wrong |
| Patch passes but the suite regresses | Revert — a fix that breaks another test is not a fix |
| Test passes intermittently | Targeted re-run ×3; 3/3 required to call it fixed |
| Suite hangs | Subprocess timeout; reported as `timed_out`, not a crash |
| Attempts exhausted | Give up on that failure, record why, continue with the rest |
| Provider refuses | Typed `Refused`, distinct from a transport error, so the agent falls back rather than retrying identically |
| Provider rate-limits | Typed `RateLimited`; the failure is recorded and the run continues instead of crashing |
| Budget reached | Checked *before* each call, so the run stops at the cap instead of reporting it blew past |
| Model names a file it was not offered | Refused at the graph, not trusted to the prompt — this is what stops a patch landing in a test file |

**Every vendor exception is translated at the adapter boundary.** This was found by
running the thing: a Gemini quota error escaped as a raw
`google.genai.errors.ClientError` and killed an entire run, because nothing above
the adapter layer knows what that type is. The coordinator already handled
`LLMError`; the error simply was never translated. Both adapters now map their SDK
errors, and both take an injectable client so the mapping is tested without a key.

Two smaller ones worth naming. Parsed ASTs are cached, so the cache is cleared
after every edit — otherwise a re-localisation ranks against pre-patch source and
reports stale line numbers. And a partially failing dependency install is logged
and the run continues: the suite may only need the subset that resolved, and
failing there turns a fixable run into no run at all.

## 5. LLM selection

**Provider is chosen per run; Anthropic is the default, Gemini the alternative.**
Both sit behind one `LLMProvider` port, so call sites request a *tier* and never
name a vendor or a model. Adding a third provider is one adapter plus a tier map.

**Routing is by task cost class, never by bug type.** Bug type is unknown at
runtime, and branching on it would hard-code exactly the knowledge the agent is
required to derive from test output.

| Tier | Anthropic | Used by |
|---|---|---|
| cheap | `claude-haiku-4-5` ($1/$5 per MTok) | Reviewer — judging material already produced |
| reasoning | `claude-opus-5` ($5/$25) | Diagnoser and Patcher — the steps that decide whether a patch is correct |

Anthropic is the default because the reasoning tier does the work that determines
correctness, and that is where the strongest available model sits. The reviewer is
a judgement call over a diff and a stated cause, not fresh analysis, so it runs on
the cheap tier — one call in three at a fifth of the price.

Retries deliberately do **not** downgrade to the cheap tier. A retry happens
because the first attempt was wrong, which is when the stronger model is most
needed; saving money on the hard case is the wrong trade. Cost is instead
controlled by the attempt budget, the queue pruning above, and the hard ledger cap.

**Cost accounting records provider + model + tokens, never tokens alone.** Prices
differ per model, so a token counter cannot answer "are we under budget". A test
asserts that two calls with *identical* token counts have different costs. Models
with no known price are recorded as **unpriced** rather than assigned a guess, so
the reported figure is never quietly wrong. Prices are injected into the ledger
rather than read from the environment inside it, keeping cost accounting
deterministic — also covered by a test.

## 6. Observability

Every run writes JSONL, one event per line, flushed as written so a killed run
still leaves a complete trace. `failure.localised` carries every candidate with
its score and reasons; `llm.call` carries provider, model, tier, tokens, USD, and
truncated prompt and response. Prompts are truncated rather than dropped — the
reasoning stays auditable without burying it in source. No API key is ever written.

## 7. Validation

| What | Result |
|---|---|
| `examples/sample_project` — six planted bugs, one per brief category | **6/6 located.** Every failing test's top patchable candidate is the exact file and symbol holding the bug |
| Single-bug repository, Gemini API | Diagnosed, patched, verified 3/3, no regressions, suite green |
| A real third-party repository (`medical-graphrag`) | Reused its existing venv, ran its suite (8 passed, 1 skipped), correctly reported nothing to fix |
| Every loop branch — accept, reject, revert, retry, exhaust, regress, prune | Covered by tests with scripted models and a scripted runner |

**Not yet demonstrated:** the full patch loop completing across all six bugs in
one run. The one attempt hit a provider `503`, which the agent handled by
recording the failure and continuing — but the end-to-end six-bug fix is unproven.

## 8. What I would improve with more time

- **Finish the six-bug fix run.** Localisation is 6/6; the remaining question is
  patch quality across all six. That is the next measurement, not a feature.
- **Confidence calibration.** Confidence is currently self-reported by the model.
  Grounding it — do high-confidence patches actually pass more often? — needs a
  corpus of runs, and would make the apply-order meaningful rather than plausible.
- **Cross-file root causes.** Ranking is per-failure. A bug whose cause spans a
  caller and a callee in different modules is found only if a frame or an import
  points there; an import-graph walk of depth > 1 would widen this.
- **Resource-leak and load-dependent bugs.** Detected only if the target's own
  suite exercises them. Reading the teardown-phase signal the plugin already
  captures would be a cheap first step.
- **Parallel localisation.** Pure AST work with no API calls, currently serial.
- **Prompt caching** on the stable system prompt, to cut cost per attempt.
- **Trace viewer.** The JSONL is complete but is read with `jq` today; a small
  renderer would make the reasoning legible to someone who did not write it.

## 9. Known limitations

- A slow target suite dominates wall time: verification is up to three targeted
  runs plus a full suite per candidate patch. `--defer-regression-check` trades
  per-patch attribution for speed, and `--only` / `--max-failures` bound the run,
  but the underlying cost is the target's own suite.
- A target configuring `-x` surfaces one failure per run; the loop still converges,
  just over more iterations.
- `gemini-2.5-pro` and `gemini-2.5-flash` are confirmed valid IDs, but Gemini
  prices are absent from the table, so those calls report as **unpriced** rather
  than as a fabricated cost. The default free tier returns `limit: 0` for
  `gemini-2.5-pro`; route the reasoning tier with
  `BUGFIXER_GEMINI_REASONING_MODEL` if that applies to your key.
- Relative imports are not resolved during callee expansion — those names fall
  back to bare-name matching with the ambiguity penalty.
- The reviewer sees the diff but not the wider file, so it cannot catch a patch
  that breaks a caller outside the excerpt. The full-suite regression gate is
  what actually catches that, one step later.
