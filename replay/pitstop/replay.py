"""Replays Pitstop's recorded steps into the trace viewer, then checks the
viewer holds them faithfully (Phase 7: the fidelity and cost checks).

    python -m replay.pitstop.replay send  STEPS_JSON --snapshot NAME [--include-content]
    python -m replay.pitstop.replay check STEPS_JSON

STEPS_JSON is what export.sql writes (run.sh does both steps). Exits
non-zero, naming each run and what differs, if anything is off."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

from replay.common import StoredSpan, fetch_trace, send_spans, to_unix_nanos
from replay.pitstop.mapping import (
    Step,
    group_runs,
    resource_for,
    span_for,
    span_id_for,
    trace_id_for,
)

# NUMERIC(12, 6) rounds each recorded cost to a millionth of a dollar.
COST_TOLERANCE_PER_CALL = Decimal("0.00001")


def load_steps(path: Path) -> list[Step]:
    return [Step.from_record(r) for r in json.loads(path.read_text())]


def send(steps: Sequence[Step], snapshot: str, include_content: bool) -> int:
    resource = resource_for(snapshot)
    spans = [
        span_for(s, resource=resource, include_content=include_content)
        for run in group_runs(steps).values()
        for s in run
    ]
    if not send_spans(spans):
        print(f"error: export of {len(spans)} spans failed", file=sys.stderr)
        return 1
    print(f"sent {len(spans)} spans in {len(group_runs(steps))} traces")
    return 0


def check_run(run: Sequence[Step], stored: Sequence[StoredSpan]) -> list[str]:
    """What differs between one run's records and the viewer's copy.
    Fidelity: every step once, same parent, same start (to the
    microsecond), same duration (to the millisecond). Cost: the model
    calls' costs add up to the run's recorded total."""
    run_id = run[0].run_id
    problems: list[str] = []
    by_id: dict[int, list[StoredSpan]] = {}
    for s in stored:
        by_id.setdefault(s.span_id, []).append(s)
    if len(stored) != len(run):
        problems.append(f"run {run_id}: {len(stored)} spans stored, {len(run)} steps recorded")
    for step in run:
        copies = by_id.get(span_id_for(step.id), [])
        if len(copies) != 1:
            problems.append(f"run {run_id}: step {step.id} stored {len(copies)} times")
            continue
        (span,) = copies
        parent = None if step.parent_id is None else span_id_for(step.parent_id)
        if span.parent_id != parent:
            problems.append(f"run {run_id}: step {step.id} has the wrong parent")
        if span.start != to_unix_nanos(step.started_at):
            problems.append(f"run {run_id}: step {step.id} starts at a different time")
        end = step.ended_at or step.started_at
        recorded_ms = (to_unix_nanos(end) - to_unix_nanos(step.started_at)) // 1_000_000
        if (span.end - span.start) // 1_000_000 != recorded_ms:
            problems.append(f"run {run_id}: step {step.id} lasts a different time")

    roots = [s for s in stored if s.parent_id is None]
    recorded = roots[0].attributes.get("pitstop.run.recorded_cost_usd") if len(roots) == 1 else None
    if recorded is not None:
        costs = [
            Decimal(str(s.attributes["agentobs.cost.usd"]))
            for s in stored
            if "agentobs.cost.usd" in s.attributes
        ]
        if abs(sum(costs, Decimal(0)) - Decimal(str(recorded))) > COST_TOLERANCE_PER_CALL * max(
            1, len(costs)
        ):
            problems.append(
                f"run {run_id}: model-call costs add up to {sum(costs)}, recorded {recorded}"
            )
    return problems


def check(
    steps: Sequence[Step], fetch: Callable[[int], list[StoredSpan]] = fetch_trace
) -> tuple[list[str], int, int]:
    """(problems, runs checked, runs whose cost was checked)."""
    problems: list[str] = []
    runs = group_runs(steps)
    costed = 0
    for run_id, run in runs.items():
        stored = fetch(trace_id_for(run_id))
        problems += check_run(run, stored)
        if any(s.kind == "run" and s.cost_usd is not None for s in run):
            costed += 1
    return problems, len(runs), costed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Pitstop's recorded traces.")
    parser.add_argument("action", choices=["send", "check"])
    parser.add_argument("steps", type=Path)
    parser.add_argument("--snapshot", default="unknown", help="the backup's file name")
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="also send summaries, free-text meta and error messages (never prompts)",
    )
    args = parser.parse_args(argv)
    steps = load_steps(args.steps)
    if args.action == "send":
        return send(steps, args.snapshot, args.include_content)
    problems, runs, costed = check(steps)
    for p in problems:
        print(p, file=sys.stderr)
    verdict = "FAIL" if problems else "ok"
    print(
        f"{verdict}: {runs} runs, {len(steps)} steps checked against the viewer; "
        f"cost checked on {costed} runs; {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
