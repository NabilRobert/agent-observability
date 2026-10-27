"""Replays Pitstop's recorded steps into the trace viewer, then checks the
viewer holds them faithfully (Phase 7: the fidelity and cost checks).

    python -m replay.pitstop.replay send  STEPS_JSON --snapshot NAME [--include-content]
    python -m replay.pitstop.replay check STEPS_JSON

STEPS_JSON is what export.sql writes (run.sh does both steps). Exits
non-zero, naming each run and what differs, if anything is off."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

from replay.pitstop.mapping import (
    Step,
    group_runs,
    resource_for,
    span_for,
    span_id_for,
    to_unix_nanos,
    trace_id_for,
)

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
QUERY = os.environ.get("JAEGER_QUERY_URL", "http://127.0.0.1:16686")
BATCH = 256
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
    exporter = OTLPSpanExporter(endpoint=f"{OTLP.rstrip('/')}/v1/traces", timeout=10)
    for i in range(0, len(spans), BATCH):
        if exporter.export(spans[i : i + BATCH]) is not SpanExportResult.SUCCESS:
            print(f"error: export failed at span {i} of {len(spans)}", file=sys.stderr)
            return 1
    exporter.shutdown()
    print(f"sent {len(spans)} spans in {len(group_runs(steps))} traces")
    return 0


@dataclass(frozen=True)
class StoredSpan:
    """One span as the viewer returns it."""

    span_id: int
    parent_id: int | None
    name: str
    start: int
    end: int
    attributes: Mapping[str, Any]


def parse_otlp_json(body: Mapping[str, Any]) -> list[StoredSpan]:
    """The spans in a Jaeger v3 API trace response (OTLP JSON)."""
    out: list[StoredSpan] = []
    for rs in body["result"]["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for s in ss["spans"]:
                parent = s.get("parentSpanId") or None
                out.append(
                    StoredSpan(
                        span_id=int(s["spanId"], 16),
                        parent_id=None if parent is None else int(parent, 16),
                        name=s["name"],
                        start=int(s["startTimeUnixNano"]),
                        end=int(s["endTimeUnixNano"]),
                        attributes={
                            a["key"]: _otlp_value(a["value"]) for a in s.get("attributes", [])
                        },
                    )
                )
    return out


def _otlp_value(value: Mapping[str, Any]) -> Any:
    if "intValue" in value:
        return int(value["intValue"])
    for key in ("doubleValue", "stringValue", "boolValue"):
        if key in value:
            return value[key]
    return value


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


def fetch_trace(trace_id: int) -> list[StoredSpan]:
    try:
        with urllib.request.urlopen(f"{QUERY}/api/v3/traces/{trace_id:032x}", timeout=10) as r:
            return parse_otlp_json(json.load(r))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


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
