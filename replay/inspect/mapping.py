"""Turns Inspect AI eval logs (the .eval files the harness publishes under
runs/) into OpenTelemetry spans, following docs/conventions.md. Pure: no
I/O beyond reading the log, so it's tested on a synthetic log.

Each sample (one task, one epoch) becomes one trace. Inspect records its
own span tree (span_begin/span_end, with parent IDs) plus model and tool
events with exact start and end times, so the trace keeps Inspect's
structure: the sample, then its solver and agent, then each model call
and tool call. IDs are derived from Inspect's own IDs, and times are the
recorded ones, unshifted."""

import json
import zipfile
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags
from opentelemetry.util.types import AttributeValue

from replay.common import derived_id, parse_time, to_unix_nanos

SCOPE = InstrumentationScope("agentobs.replay.inspect", "0.1.0")
_FLAGS = TraceFlags(TraceFlags.SAMPLED)


@dataclass(frozen=True)
class Sample:
    """One sample of one eval run, as recorded."""

    run: str
    eval_id: str
    task: str
    model: str
    data: Mapping[str, Any]


def read_eval(path: Path, run: str | None = None) -> list[Sample]:
    """The samples in an .eval file (a zip of JSON documents)."""
    with zipfile.ZipFile(path) as z:
        header = json.loads(z.read("header.json"))
        names = sorted(n for n in z.namelist() if n.startswith("samples/"))
        samples = [json.loads(z.read(n)) for n in names]
    ev = header["eval"]
    return [Sample(run or path.stem, ev["eval_id"], ev["task"], ev["model"], s) for s in samples]


def resource_for(snapshot: str) -> Resource:
    return Resource.create(
        {
            "service.name": "agent-eval-harness",
            "agentobs.replay": True,
            "agentobs.replay.snapshot": snapshot,
        }
    )


def trace_id_for(sample: Sample) -> int:
    return derived_id(f"inspect/{sample.data['uuid']}", 16)


def _span_id(key: str) -> int:
    return derived_id(f"inspect/{key}", 8)


def _model_parts(model: str) -> tuple[str, str]:
    """('openai', 'gpt-5-mini') from 'openai-api/sumopod/gpt-5-mini'."""
    provider, _, rest = model.partition("/")
    return provider.removesuffix("-api"), rest.rsplit("/", 1)[-1] or model


def spans_for(
    sample: Sample, *, resource: Resource, include_content: bool = False
) -> list[ReadableSpan]:
    """Every span of one sample's trace: the sample's root span first."""
    data = sample.data
    trace_id = trace_id_for(sample)
    root_id = _span_id(f"{data['uuid']}/sample")
    sample_start = to_unix_nanos(parse_time(data["started_at"]))
    sample_end = to_unix_nanos(parse_time(data["completed_at"]))
    events: list[Mapping[str, Any]] = data["events"]

    begins = {e["id"]: e for e in events if e["event"] == "span_begin"}
    ends = {e["id"]: e for e in events if e["event"] == "span_end"}
    # Each Inspect "tool" span wraps one tool event, recorded on the span's
    # parent; matched in order by (parent span, tool name).
    tool_events: dict[tuple[str, str], deque[Mapping[str, Any]]] = {}
    for e in events:
        if e["event"] == "tool":
            tool_events.setdefault((e["span_id"], e["function"]), deque()).append(e)

    def context(span_id: int) -> SpanContext:
        return SpanContext(trace_id, span_id, is_remote=False, trace_flags=_FLAGS)

    def parent_of(inspect_span: str | None) -> SpanContext:
        if inspect_span is not None and inspect_span in begins:
            return context(_span_id(inspect_span))
        return context(root_id)

    def make(
        name: str,
        span_id: int,
        parent: SpanContext | None,
        start: int,
        end: int,
        attributes: dict[str, AttributeValue],
        kind: SpanKind = SpanKind.INTERNAL,
        status: Status | None = None,
    ) -> ReadableSpan:
        return ReadableSpan(
            name=name,
            context=context(span_id),
            parent=parent,
            resource=resource,
            attributes=attributes,
            kind=kind,
            status=status or Status(StatusCode.UNSET),
            start_time=start,
            end_time=end,
            instrumentation_scope=SCOPE,
        )

    spans = [
        make(
            f"eval sample {data['id']}",
            root_id,
            None,
            sample_start,
            sample_end,
            _sample_attributes(sample),
        )
    ]

    for span_key, begin in begins.items():
        start = to_unix_nanos(parse_time(begin["timestamp"]))
        end_event = ends.get(span_key)
        end = to_unix_nanos(parse_time(end_event["timestamp"])) if end_event else sample_end
        kind_name, name = begin.get("type") or "", begin["name"]
        attrs: dict[str, AttributeValue] = {"inspect.span.type": kind_name}
        if end_event is None:
            attrs["inspect.span.unfinished"] = True
        status = None
        if kind_name == "agent":
            attrs["gen_ai.operation.name"] = "invoke_agent"
            attrs["gen_ai.agent.name"] = name
            span_name = f"invoke_agent {name}"
        elif kind_name == "tool":
            attrs.update(
                {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": name,
                    "gen_ai.tool.type": "function",
                }
            )
            queue = tool_events.get((begin.get("parent_id") or "", name))
            event = queue.popleft() if queue else None
            if event is not None:
                attrs["gen_ai.tool.call.id"] = event["id"]
                if include_content:
                    attrs["gen_ai.tool.call.arguments"] = json.dumps(event.get("arguments"))
                status = _tool_status(event, attrs, include_content)
            span_name = f"execute_tool {name}"
        else:
            span_name = name if name == kind_name or not kind_name else f"{kind_name} {name}"
        spans.append(
            make(
                span_name,
                _span_id(span_key),
                parent_of(begin.get("parent_id")),
                start,
                end,
                attrs,
                status=status,
            )
        )

    for e in events:
        if e["event"] == "model":
            name, start, end, attrs, status = _model_call(e, include_content)
            spans.append(
                make(
                    name,
                    _span_id(e["uuid"]),
                    parent_of(e.get("span_id")),
                    start,
                    end,
                    attrs,
                    kind=SpanKind.CLIENT,
                    status=status,
                )
            )
    return spans


def _sample_attributes(sample: Sample) -> dict[str, AttributeValue]:
    data = sample.data
    provider, model = _model_parts(sample.model)
    attrs: dict[str, AttributeValue] = {
        "inspect.run": sample.run,
        "inspect.eval_id": sample.eval_id,
        "inspect.task": sample.task,
        "inspect.sample.id": str(data["id"]),
        "inspect.epoch": int(data["epoch"]),
        "inspect.model": sample.model,
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
    }
    # The sample's own totals, kept apart from the model calls' cost so
    # that summing a trace never counts anything twice (ADR-004). The
    # replay's check compares the two.
    usage: Mapping[str, Mapping[str, Any]] = data.get("model_usage") or {}
    attrs["inspect.recorded.cost_usd"] = sum(
        float(u.get("total_cost") or 0) for u in usage.values()
    )
    attrs["inspect.recorded.input_tokens"] = sum(int(u["input_tokens"]) for u in usage.values())
    attrs["inspect.recorded.output_tokens"] = sum(int(u["output_tokens"]) for u in usage.values())
    # Scores: a scorer's value is a number, a string, or a dict of them.
    scores: Mapping[str, Mapping[str, Any]] = data.get("scores") or {}
    for scorer, score in scores.items():
        value: Any = score.get("value")
        values: Mapping[str, Any] = (
            cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {"value": value}
        )
        for key, v in values.items():
            if isinstance(v, int | float | str | bool):
                attrs[f"inspect.score.{scorer}.{key}"] = v
    limit: Mapping[str, Any] | None = data.get("limit")
    if limit:
        attrs["inspect.limit.type"] = str(limit.get("type"))
        attrs["inspect.limit.value"] = float(limit.get("limit") or 0)
    return attrs


def _tool_status(
    event: Mapping[str, Any], attrs: dict[str, AttributeValue], include_content: bool
) -> Status | None:
    error = event.get("error")
    if not error:
        return None
    attrs["error.type"] = f"inspect.tool_{error.get('type') or 'error'}"
    return Status(StatusCode.ERROR, error.get("message") if include_content else None)


def _model_call(
    e: Mapping[str, Any], include_content: bool
) -> tuple[str, int, int, dict[str, AttributeValue], Status | None]:
    """Name, start, end, attributes and status of one model call."""
    provider, model = _model_parts(e["model"])
    start = to_unix_nanos(parse_time(e["timestamp"]))
    attrs: dict[str, AttributeValue] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "inspect.model": e["model"],
    }
    if e.get("completed"):
        end = to_unix_nanos(parse_time(e["completed"]))
    else:
        end = start
        attrs["inspect.event.unfinished"] = True
    output: Mapping[str, Any] = e.get("output") or {}
    usage: Mapping[str, Any] = output.get("usage") or {}
    for key, attr in (
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("input_tokens_cache_read", "gen_ai.usage.cache_read.input_tokens"),
        ("reasoning_tokens", "gen_ai.usage.reasoning.output_tokens"),
    ):
        if isinstance(usage.get(key), int):
            attrs[attr] = usage[key]
    if isinstance(usage.get("total_cost"), int | float):
        attrs["agentobs.cost.usd"] = float(usage["total_cost"])
    choices: list[Mapping[str, Any]] = output.get("choices") or []
    reasons = tuple(str(c["stop_reason"]) for c in choices if c.get("stop_reason"))
    if reasons:
        attrs["gen_ai.response.finish_reasons"] = reasons
    if output.get("response_id"):
        attrs["gen_ai.response.id"] = str(output["response_id"])
    status = None
    if e.get("error"):
        message = str(e["error"])
        attrs["error.type"] = message.split("(", 1)[0] or "inspect.model_error"
        status = Status(StatusCode.ERROR, message if include_content else None)
    return f"chat {model}", start, end, attrs, status
