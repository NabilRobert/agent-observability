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


@dataclass
class _Draft:
    """One span before it's placed in its trace: what the builders below
    return, so that none of them needs the trace ID or the resource."""

    name: str
    span_id: int
    parent_id: int | None  # None only for the sample's root span
    start: int
    end: int
    attributes: dict[str, AttributeValue]
    kind: SpanKind = SpanKind.INTERNAL
    status: Status | None = None


@dataclass(frozen=True)
class _SampleEvents:
    """One sample's events, indexed the way the builders below need them."""

    root_id: int
    end: int
    begins: dict[str, Mapping[str, Any]]
    ends: dict[str, Mapping[str, Any]]
    # Each Inspect "tool" span wraps one tool event, recorded on the span's
    # parent; matched in order by (parent span, tool name).
    tools: dict[tuple[str, str], deque[Mapping[str, Any]]]

    @classmethod
    def index(cls, data: Mapping[str, Any]) -> _SampleEvents:
        events: list[Mapping[str, Any]] = data["events"]
        tools: dict[tuple[str, str], deque[Mapping[str, Any]]] = {}
        for e in events:
            if e["event"] == "tool":
                tools.setdefault((e["span_id"], e["function"]), deque()).append(e)
        return cls(
            root_id=_span_id(f"{data['uuid']}/sample"),
            end=to_unix_nanos(parse_time(data["completed_at"])),
            begins={e["id"]: e for e in events if e["event"] == "span_begin"},
            ends={e["id"]: e for e in events if e["event"] == "span_end"},
            tools=tools,
        )

    def parent_of(self, inspect_span: str | None) -> int:
        if inspect_span is not None and inspect_span in self.begins:
            return _span_id(inspect_span)
        return self.root_id


def spans_for(
    sample: Sample, *, resource: Resource, include_content: bool = False
) -> list[ReadableSpan]:
    """Every span of one sample's trace: the sample's root span first, then
    Inspect's own spans, then the model calls, each in recorded order."""
    data = sample.data
    indexed = _SampleEvents.index(data)
    root = _Draft(
        f"eval sample {data['id']}",
        indexed.root_id,
        None,
        to_unix_nanos(parse_time(data["started_at"])),
        indexed.end,
        _sample_attributes(sample),
    )
    drafts = [root]
    for key, begin in indexed.begins.items():
        drafts.append(_inspect_span(indexed, key, begin, include_content))
    for e in data["events"]:
        if e["event"] == "model":
            drafts.append(_model_call(e, indexed.parent_of(e.get("span_id")), include_content))
    trace_id = trace_id_for(sample)
    return [_readable(d, trace_id, resource) for d in drafts]


def _readable(draft: _Draft, trace_id: int, resource: Resource) -> ReadableSpan:
    def context(span_id: int) -> SpanContext:
        return SpanContext(trace_id, span_id, is_remote=False, trace_flags=_FLAGS)

    return ReadableSpan(
        name=draft.name,
        context=context(draft.span_id),
        parent=None if draft.parent_id is None else context(draft.parent_id),
        resource=resource,
        attributes=draft.attributes,
        kind=draft.kind,
        status=draft.status or Status(StatusCode.UNSET),
        start_time=draft.start,
        end_time=draft.end,
        instrumentation_scope=SCOPE,
    )


def _inspect_span(
    indexed: _SampleEvents, key: str, begin: Mapping[str, Any], include_content: bool
) -> _Draft:
    """One of Inspect's own spans (solver, agent, tool, scorer, ...)."""
    end_event = indexed.ends.get(key)
    kind_name, name = begin.get("type") or "", begin["name"]
    draft = _Draft(
        name,
        _span_id(key),
        indexed.parent_of(begin.get("parent_id")),
        to_unix_nanos(parse_time(begin["timestamp"])),
        to_unix_nanos(parse_time(end_event["timestamp"])) if end_event else indexed.end,
        {"inspect.span.type": kind_name},
    )
    if end_event is None:
        draft.attributes["inspect.span.unfinished"] = True
    if kind_name == "agent":
        draft.name = f"invoke_agent {name}"
        draft.attributes["gen_ai.operation.name"] = "invoke_agent"
        draft.attributes["gen_ai.agent.name"] = name
    elif kind_name == "tool":
        queue = indexed.tools.get((begin.get("parent_id") or "", name))
        _add_tool_call(draft, name, queue.popleft() if queue else None, include_content)
    elif kind_name and name != kind_name:
        draft.name = f"{kind_name} {name}"
    return draft


def _add_tool_call(
    draft: _Draft, name: str, event: Mapping[str, Any] | None, include_content: bool
) -> None:
    draft.name = f"execute_tool {name}"
    attrs = draft.attributes
    attrs["gen_ai.operation.name"] = "execute_tool"
    attrs["gen_ai.tool.name"] = name
    attrs["gen_ai.tool.type"] = "function"
    if event is None:
        return
    attrs["gen_ai.tool.call.id"] = event["id"]
    if include_content:
        attrs["gen_ai.tool.call.arguments"] = json.dumps(event.get("arguments"))
    draft.status = _tool_status(event, attrs, include_content)


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


def _model_call(e: Mapping[str, Any], parent_id: int, include_content: bool) -> _Draft:
    """One model call, as a client span."""
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
    return _Draft(
        f"chat {model}", _span_id(e["uuid"]), parent_id, start, end, attrs, SpanKind.CLIENT, status
    )
