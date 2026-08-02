"""Primary adapter: command line.

Translation only -- parse arguments, invoke a use case, render the result. All
orchestration lives in `application/`, so the agent can be driven programmatically
or from another front end without going through argparse.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from datetime import datetime, timezone

from . import __version__
from .application.diagnose import Diagnosis
from .application.graph import DEFAULT_MAX_ATTEMPTS, DEFAULT_MIN_CONFIDENCE
from .application.verify import DEFAULT_REPEATS
from .bootstrap import build_diagnose, build_fix, build_ledger, build_tracer
from .config import PROVIDERS, ConfigError, load_settings
from .domain.trace import Kind
from .ports import PortError

log = logging.getLogger("bugfixer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugfixer",
        description="Diagnose failing pytest suites in any repository.",
    )
    parser.add_argument("target", help="local path or clone URL of the repository to work on")
    parser.add_argument(
        "--workspace",
        default="workspace",
        help="directory for clones and provisioned environments (default: ./workspace)",
    )
    parser.add_argument(
        "--python",
        help="interpreter to run the target's tests with (default: detect or provision)",
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="per-test-run timeout in seconds"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="attempt to fix the failures, not just locate them (requires an API key)",
    )
    parser.add_argument(
        "--provider", choices=PROVIDERS, help="LLM provider for --fix (default: anthropic)"
    )
    parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
        help="patch attempts per failing test",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
        help="reviewer confidence below which a patch is not applied",
    )
    parser.add_argument(
        "--verify-repeats", type=int, default=DEFAULT_REPEATS,
        help="targeted re-runs used to tell a fix from a flaky pass",
    )
    parser.add_argument(
        "--only", metavar="NODEID",
        help="attempt only this failing test (exact node id, or a substring of one)",
    )
    parser.add_argument(
        "--max-failures", type=int, metavar="N",
        help="attempt at most N failing tests, strongest localisation first",
    )
    parser.add_argument(
        "--defer-regression-check", action="store_true",
        help="skip the full suite after each patch and check once at the end "
             "(much faster on slow suites; a regression cannot be attributed to one patch)",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--trace",
        help="write the run trace here (default: <workspace>/runs/<timestamp>.jsonl)",
    )
    parser.add_argument("--no-trace", action="store_true", help="disable trace recording")
    parser.add_argument("-v", "--verbose", action="store_true", help="show progress logging")
    parser.add_argument("--version", action="version", version=f"bugfixer {__version__}")
    return parser


def trace_path(args: argparse.Namespace, workspace: Path) -> Path | None:
    if args.no_trace:
        return None
    if args.trace:
        return Path(args.trace).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return workspace / "runs" / f"{stamp}.jsonl"


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def to_report(diagnosis: Diagnosis) -> dict:
    """Flatten a Diagnosis into JSON-safe primitives for rendering."""
    suite = diagnosis.suite
    return {
        "target": str(diagnosis.repo.root),
        "cloned": diagnosis.repo.cloned,
        "interpreter": diagnosis.environment.describe(),
        "suite": suite.summary(),
        "duration_seconds": round(suite.duration, 1),
        "green": diagnosis.green,
        "timed_out": suite.timed_out,
        "collection_errors": [e["nodeid"] for e in suite.collection_errors],
        "failures": [
            {
                "nodeid": failure.nodeid,
                "phase": failure.phase,
                "message": failure.message.strip(),
                "candidates": [
                    {
                        "path": excerpt.path,
                        "symbol": excerpt.symbol,
                        "lines": [excerpt.start_line, excerpt.end_line],
                        "score": excerpt.score,
                        "patchable": excerpt.patchable,
                        "reasons": list(excerpt.reasons),
                    }
                    for excerpt in found.excerpts
                ],
            }
            for failure, found in zip(suite.failures, diagnosis.localisations)
        ],
    }


def render(report: dict) -> str:
    lines = [
        f"Target       {report['target']}",
        f"Interpreter  {report['interpreter']}",
        f"Suite        {report['suite']}  ({report['duration_seconds']}s)",
        "",
    ]

    if report["collection_errors"]:
        lines.append("Collection errors (the suite could not be fully loaded):")
        lines += [f"  - {nodeid}" for nodeid in report["collection_errors"]]
        lines.append("")

    if report["green"]:
        lines.append("Suite is green - nothing to fix.")
        return "\n".join(lines)

    if not report["failures"]:
        lines.append("No individual test failures were reported.")
        return "\n".join(lines)

    total = len(report["failures"])
    lines.append(f"{total} failing test(s):")
    for position, failure in enumerate(report["failures"], start=1):
        lines.append("")
        lines.append(f"[{position}/{total}] {failure['nodeid']}  ({failure['phase']})")
        if failure["message"]:
            lines.append(f"        {failure['message'].splitlines()[0]}")

        if not failure["candidates"]:
            lines.append("        no candidate code located")
            continue

        lines.append("        candidates, most likely first:")
        for candidate in failure["candidates"]:
            marker = " " if candidate["patchable"] else "*"
            location = f"{candidate['path']}:{candidate['lines'][0]}-{candidate['lines'][1]}"
            lines.append(
                f"        {marker} {candidate['score']:>5}  {location}  {candidate['symbol']}"
            )
            for reason in candidate["reasons"]:
                lines.append(f"                  - {reason}")

    lines += ["", "* context only - test files are never modified."]
    return "\n".join(lines)


def render_fix(outcome, report: dict) -> str:
    lines = [render(report), ""]

    if outcome.fixed:
        lines.append(f"Fixed {len(outcome.fixed)} test(s):")
        lines += [f"  + {nodeid}" for nodeid in outcome.fixed]
    if outcome.unfixed:
        lines.append(f"Could not fix {len(outcome.unfixed)} test(s):")
        for entry in outcome.unfixed:
            lines.append(f"  - {entry['nodeid']}")
            lines.append(f"      {entry['reason']} (after {entry['attempts']} attempt(s))")

    final = outcome.final_suite
    if final is not None:
        lines += ["", f"Final suite: {final.summary()}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    destination = trace_path(args, workspace)
    tracer = build_tracer(destination)

    try:
        if args.fix:
            settings = load_settings(args.provider)
            ledger = build_ledger(settings)
            fix = build_fix(
                settings,
                ledger,
                tracer,
                repeats=args.verify_repeats,
                check_regressions=not args.defer_regression_check,
            )
            outcome = fix(
                target=args.target,
                workspace=workspace,
                python=args.python,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                min_confidence=args.min_confidence,
                only=args.only,
                max_failures=args.max_failures,
            )
            diagnosis, green = outcome.diagnosis, outcome.green
        else:
            outcome, ledger = None, None
            diagnosis = build_diagnose(tracer)(
                target=args.target,
                workspace=workspace,
                python=args.python,
                timeout=args.timeout,
            )
            green = diagnosis.green
    except ConfigError as error:
        tracer.sink.close()
        print(f"bugfixer: {error}", file=sys.stderr)
        return 2
    except PortError as error:
        tracer.emit(Kind.ERROR, error=type(error).__name__, detail=str(error))
        tracer.sink.close()
        print(f"bugfixer: {error}", file=sys.stderr)
        return 2

    report = to_report(diagnosis)
    if outcome is None:
        tracer.emit(
            Kind.RUN_FINISHED,
            green=green,
            failing=len(report["failures"]),
            duration_seconds=report["duration_seconds"],
        )
    tracer.sink.close()

    if args.json:
        if outcome is not None:
            report["fixed"] = list(outcome.fixed)
            report["unfixed"] = list(outcome.unfixed)
            report["final_green"] = green
            report["cost"] = ledger.summary()
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_fix(outcome, report) if outcome is not None else render(report))
        if ledger is not None:
            spent = ledger.summary()
            print(
                f"\nCost: ${spent['total_usd']:.4f} of ${spent['max_cost_usd']:.2f} "
                f"across {spent['calls']} call(s)"
            )
        if destination is not None:
            print(f"Trace written to {destination}")

    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
