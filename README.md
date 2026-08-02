# bugfixer

An autonomous agent that diagnoses and fixes bugs in a repository with a failing
pytest suite. Point it at any repo — local path or clone URL — and it runs the suite,
localises each failure's root cause, patches it, re-verifies, reverts and retries on
failure, and loops until green or until it reports what it could not fix and why.

Diagnosis needs no API key. Fixing needs one — see [Fixing](#fixing).

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

### Environment variables

Only needed once patch generation lands; diagnosis is entirely deterministic.
Copy `.env.example` to `.env` and fill in the key for the provider you intend to use.

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | for `--provider anthropic` (default) | Anthropic API key |
| `GEMINI_API_KEY` | for `--provider gemini` | Google Gemini API key |
| `BUGFIXER_PROVIDER` | no | `anthropic` (default) or `gemini` |
| `BUGFIXER_MAX_COST_USD` | no | Run budget, default `5.00` |
| `BUGFIXER_ANTHROPIC_REASONING_MODEL` | no | Override the model for a tier |
| `BUGFIXER_PRICE_<MODEL>_INPUT` / `_OUTPUT` | no | USD per million tokens for a model the price table doesn't know |

`.env` is gitignored. No key is ever written to a trace or a log.

## Reproduce a run

One command:

```bash
uv run bugfixer <path-or-clone-url>
```

Real output against a repo with one failing test:

```
Target       /tmp/demo
Interpreter  /tmp/demo/.venv/bin/python (existing)
Suite        1 failed  (2.2s)

1 failing test(s):

[1/1] test_pricing.py::test_apply_discount  (call)
        assert 190 == 180
        candidates, most likely first:
            7.5  pricing.py:1-2  apply_discount
                  - test_apply_discount asserts on apply_discount(), imported from pricing
                  - failure text mentions apply_discount
        *   5.0  test_pricing.py:3-4  test_apply_discount
                  - traceback frame in test test_pricing.py

* context only - test files are never modified.

Trace written to /tmp/demo/ws/runs/20260802T202950Z.jsonl
```

## Fixing

Add `--fix` to patch what it finds. This calls an LLM and needs an API key.

```bash
uv run bugfixer <path-or-clone-url> --fix
```

A real run (Gemini, one planted bug):

```
Suite        1 failed  (5.2s)
...
Fixed 1 test(s):
  + test_pricing.py::test_apply_discount

Final suite: 1 passed

Cost: $0.0000 of $5.00 across 3 call(s)
Trace written to .../runs/20260802T204633Z.jsonl
```

It applied:

```diff
 def apply_discount(total, percent):
-    return total - percent
+    discount_amount = total * (percent / 100)
+    return total - discount_amount
```

The loop per failing test is **diagnose → patch → review → apply → verify → decide**,
coordinated by a LangGraph state machine. A patch is applied only if the reviewer
approves it, and is reverted unless the targeted test passes consistently *and* the
full suite shows no new failures. Failed attempts are fed back as context to the next
one, bounded by `--max-attempts`.

Failures are attempted strongest-localisation first, and once a patch is accepted,
any other queued failure it also cleared is dropped without reaching a model —
failing tests are not one-to-one with bugs.

### On large or slow repositories

The target's own suite dominates wall time. Take a small bite first:

```bash
uv run bugfixer <repo> --fix --only test_apply_discount   # one test
uv run bugfixer <repo> --fix --max-failures 2             # two, best-ranked first
uv run bugfixer <repo> --fix --defer-regression-check     # one full suite at the end
```

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--fix` | off | Patch the failures, not just locate them |
| `--provider` | `anthropic` | `anthropic` or `gemini` |
| `--max-attempts N` | `3` | Patch attempts per failing test |
| `--min-confidence F` | `0.5` | Reviewer confidence below which a patch is not applied |
| `--verify-repeats N` | `3` | Targeted re-runs used to tell a fix from a flaky pass |
| `--only NODEID` | all | Attempt one failing test (exact node id or a substring) |
| `--max-failures N` | all | Attempt at most N, strongest localisation first |
| `--defer-regression-check` | off | Skip the per-patch full suite; check once at the end |
| `--workspace DIR` | `./workspace` | Where clones, provisioned venvs, and traces go |
| `--python PATH` | detect or provision | Interpreter to run the target's suite with |
| `--timeout N` | `600` | Per-test-run timeout in seconds |
| `--json` | off | Emit the report as JSON |
| `--trace PATH` | `<workspace>/runs/<timestamp>.jsonl` | Where to write the run trace |
| `--no-trace` | off | Disable trace recording |
| `-v` | off | Progress logging on stderr |

Exit codes: `0` suite green, `1` failures remain, `2` the run could not start.

## How the target is run

The agent never installs itself into the target and never edits its files to
observe it:

- **Interpreter** — an existing `.venv` in the target is reused if it has pytest;
  otherwise `uv` provisions one under `<workspace>/envs/<repo>/`, so no `.venv`
  is left behind in a repo you pointed it at.
- **Results** — collected by a pytest plugin injected via `-p` and `PYTHONPATH`,
  so the target gains no dependency. `-p no:cacheprovider` keeps `.pytest_cache`
  out of the target too.
- **Test files are never modified.** They appear in reports as context only,
  marked with `*`.

## Run trace

Every run writes JSONL — one event per line, flushed as it happens, so a trace is
complete and readable even if the run is killed. Event kinds:

```
run.started · repo.acquired · environment.ready · suite.run · failure.localised
llm.call · llm.refused · budget.exceeded · patch.proposed · patch.applied
patch.reverted · verification · decision · run.finished · error
```

Each `failure.localised` event carries every ranked candidate with its score and
the reasons it ranked, which is the audit trail for why the agent believed a given
root cause. Each `llm.call` carries provider, model, tier, token counts and USD cost.
`patch.proposed` carries the full unified diff.

A complete trace from a successful fix run is committed at
[`docs/example-run-trace.jsonl`](docs/example-run-trace.jsonl) — 15 events covering
diagnosis, all three model calls with token counts, the proposed diff, the applied
patch, verification, and the accept decision. It reads as:

```
 1  run.started
 2  repo.acquired
 3  environment.ready
 4  suite.run
 5  failure.localised
 6  llm.call           diagnose gemini-2.5-flash in=374 out=481
 7  decision           diagnosed
 8  llm.call           patch    gemini-2.5-flash in=543 out=227
 9  patch.proposed     pricing.py::apply_discount
10  llm.call           review   gemini-2.5-flash in=477 out=979
11  decision           review
12  patch.applied
13  verification       fixed: 3/3 targeted runs passed, no regressions
14  decision           accepted
15  run.finished
```

## Development

```bash
uv run pytest                                   # full suite (166 tests)
uv run pytest tests/test_fix_loop.py            # one file
uv run pytest -k callee_expansion               # one test
uv run pytest tests/test_architecture.py        # layering rules
```

`tests/test_architecture.py` enforces the dependency rules by parsing the import
graph: it fails the build if `domain/` imports an SDK, `application/` imports an
adapter, or an adapter is constructed outside the composition root.

Most tests run against in-memory fakes and finish in milliseconds; the ones that
shell out to real pytest are what make the suite take ~45s.

Every path through the coordinator — accept, reject, revert, retry, exhaust,
regress — is covered by tests driven with scripted models and a scripted runner,
so no API key or network is needed to exercise them. A flaky test is
`FakeSuiteRunner(failing, passing, passing)`.

## Try it on the sample

`examples/sample_project` is a small orders service with **six planted bugs**, one
per category in the brief (off-by-one, missing validation, async race, data
transformation, SQL injection, resource leak). Standard library only, no install.

```bash
uv run bugfixer examples/sample_project
```

The agent locates **6/6** — every failing test's top candidate is the exact file
and symbol holding the bug. It has no knowledge of that project; nothing under
`src/bugfixer/` refers to it.

## Verified against

| What | Result |
|---|---|
| `examples/sample_project`, six planted bugs | 6/6 located correctly |
| Single-bug repository, Gemini API | Diagnosed, patched, verified, green |
| A real third-party repository | Reused its venv, ran its suite, reported nothing to fix |

**Not yet demonstrated:** the full patch loop completing across all six bugs in one
run — the one attempt hit a provider `503`, which the agent recorded and continued
past. See [DESIGN.md](DESIGN.md) for architecture and rationale.
