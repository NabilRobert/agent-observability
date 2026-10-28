"""Replays the harness's recorded Inspect runs into the trace viewer, then
checks the viewer holds them faithfully.

    python -m replay.inspect.replay send  EVAL_FILE... [--snapshot LABEL] [--include-content]
    python -m replay.inspect.replay check EVAL_FILE...

run.sh extracts the published runs from the harness's git history and
does both. Exits non-zero, naming each sample and what differs, if
anything is off."""

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from replay.common import StoredSpan, compare_spans, fetch_trace, send_spans
from replay.inspect.mapping import Sample, read_eval, resource_for, spans_for, trace_id_for

# Each model call's cost is a float; their sum can differ from the
# sample's recorded total in the last few bits.
COST_TOLERANCE = 1e-9


def load(paths: Sequence[Path]) -> list[Sample]:
    return [s for p in paths for s in read_eval(p)]


def send(samples: Sequence[Sample], snapshot: str | None, include_content: bool) -> int:
    spans = [
        span
        for s in samples
        for span in spans_for(
            s,
            resource=resource_for(f"{snapshot} runs/{s.run}" if snapshot else f"runs/{s.run}"),
            include_content=include_content,
        )
    ]
    if not send_spans(spans):
        print(f"error: export of {len(spans)} spans failed", file=sys.stderr)
        return 1
    print(f"sent {len(spans)} spans in {len(samples)} traces")
    return 0


def check_sample(sample: Sample, stored: Sequence[StoredSpan]) -> list[str]:
    """Fidelity (every span once, same parent and times) and usage: the
    model calls' cost and tokens add up to the sample's recorded totals."""
    label = f"{sample.run} sample {sample.data['id']} epoch {sample.data['epoch']}"
    problems = compare_spans(label, spans_for(sample, resource=resource_for("")), stored)
    roots = [s for s in stored if s.parent_id is None]
    if len(roots) != 1:
        return [*problems, f"{label}: {len(roots)} root spans"]
    root = roots[0].attributes
    calls = [s.attributes for s in stored if s.attributes.get("gen_ai.operation.name") == "chat"]
    cost = math.fsum(float(a.get("agentobs.cost.usd", 0.0)) for a in calls)
    if abs(cost - float(root["inspect.recorded.cost_usd"])) > COST_TOLERANCE:
        problems.append(
            f"{label}: model calls cost {cost}, recorded {root['inspect.recorded.cost_usd']}"
        )
    for kind in ("input", "output"):
        tokens = sum(int(a.get(f"gen_ai.usage.{kind}_tokens", 0)) for a in calls)
        if tokens != root[f"inspect.recorded.{kind}_tokens"]:
            problems.append(
                f"{label}: model calls used {tokens} {kind} tokens, "
                f"recorded {root[f'inspect.recorded.{kind}_tokens']}"
            )
    return problems


def check(
    samples: Sequence[Sample], fetch: Callable[[int], list[StoredSpan]] = fetch_trace
) -> list[str]:
    problems: list[str] = []
    for sample in samples:
        problems += check_sample(sample, fetch(trace_id_for(sample)))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the harness's recorded Inspect runs.")
    parser.add_argument("action", choices=["send", "check"])
    parser.add_argument("evals", type=Path, nargs="+", help=".eval files")
    parser.add_argument(
        "--snapshot", help="where the logs came from, e.g. a repo and commit; runs/<name> follows"
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="also send tool-call arguments and error messages (never prompts or outputs)",
    )
    args = parser.parse_args(argv)
    samples = load(args.evals)
    if args.action == "send":
        return send(samples, args.snapshot, args.include_content)
    problems = check(samples)
    for p in problems:
        print(p, file=sys.stderr)
    spans = sum(len(spans_for(s, resource=resource_for(""))) for s in samples)
    print(
        f"{'FAIL' if problems else 'ok'}: {len(args.evals)} runs, {len(samples)} samples, "
        f"{spans} spans checked against the viewer, with cost and tokens; "
        f"{len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
