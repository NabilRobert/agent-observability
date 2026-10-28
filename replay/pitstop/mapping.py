"""Turns Pitstop's recorded steps (rows of its `trace_events` table) into
OpenTelemetry spans, following docs/conventions.md. Pure: no I/O, so the
whole mapping is tested on a synthetic fixture.

Each pipeline run becomes one trace. IDs are derived from the run and
step IDs, so replaying the same snapshot twice gives the same IDs, and
the times are the recorded ones, unshifted (Phase 5, ADR-006)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags
from opentelemetry.util.types import AttributeValue

from replay.common import derived_id, parse_time, to_unix_nanos

SCOPE = InstrumentationScope("agentobs.replay.pitstop", "0.1.0")

# String values in `meta` that are labels, not content: safe to record
# without content capture. Every other string (feedback, reasons, dates,
# email subjects, stack traces) is content (docs/conventions.md, rule 5).
LABEL_KEYS = frozenset({"agent_type", "final_status", "outcome", "grade", "run_type"})

# llm_call meta keys mapped to GenAI attributes below, so not repeated
# as pitstop.meta.*.
_LLM_KEYS = frozenset(
    {
        "attempt",
        "cached_tokens",
        "finish_reason",
        "json",
        "max_completion_tokens",
        "reasoning_effort",
        "reasoning_tokens",
    }
)


_CRITIC_KEYS = {
    "pass": "pitstop.critic.pass",
    "agent_type": "pitstop.critic.reviewed_agent",
    "grade": "pitstop.critic.grade",
    "has_issues": "pitstop.critic.has_issues",
}


class ReplayError(Exception):
    """The records don't form one tree per run."""


@dataclass(frozen=True)
class Step:
    """One recorded step, as exported by export.sql."""

    id: int
    run_id: int
    parent_id: int | None
    seq: int
    kind: str
    name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    summary: str | None
    meta: Mapping[str, Any]
    error_message: str | None

    @classmethod
    def from_record(cls, r: Mapping[str, Any]) -> "Step":
        return cls(
            id=int(r["id"]),
            run_id=int(r["run_id"]),
            parent_id=None if r["parent_id"] is None else int(r["parent_id"]),
            seq=int(r["seq"]),
            kind=str(r["kind"]),
            name=str(r["name"]),
            status=str(r["status"]),
            started_at=parse_time(r["started_at"]),
            ended_at=None if r["ended_at"] is None else parse_time(r["ended_at"]),
            model=r["model"] or None,
            tokens_in=r["tokens_in"],
            tokens_out=r["tokens_out"],
            cost_usd=None if r["cost_usd"] is None else Decimal(str(r["cost_usd"])),
            summary=r["summary"],
            meta=r["meta"] or {},
            error_message=r["error_message"],
        )


def trace_id_for(run_id: int) -> int:
    return derived_id(f"pitstop/run/{run_id}", 16)


def span_id_for(step_id: int) -> int:
    return derived_id(f"pitstop/step/{step_id}", 8)


def resource_for(snapshot: str) -> Resource:
    return Resource.create(
        {
            "service.name": "pitstop",
            "agentobs.replay": True,
            "agentobs.replay.snapshot": snapshot,
        }
    )


def group_runs(steps: Iterable[Step]) -> dict[int, list[Step]]:
    """Steps by run, in recorded order, after checking each run is one
    tree: exactly one root, and every parent inside the same run."""
    runs: dict[int, list[Step]] = {}
    for step in steps:
        runs.setdefault(step.run_id, []).append(step)
    for run_id, run_steps in runs.items():
        run_steps.sort(key=lambda s: s.seq)
        ids = {s.id for s in run_steps}
        roots = [s for s in run_steps if s.parent_id is None]
        if len(roots) != 1:
            raise ReplayError(f"run {run_id} has {len(roots)} roots, not 1")
        orphans = [s.id for s in run_steps if s.parent_id is not None and s.parent_id not in ids]
        if orphans:
            raise ReplayError(f"run {run_id}: steps {orphans} have a parent outside the run")
    return runs


def span_for(step: Step, *, resource: Resource, include_content: bool = False) -> ReadableSpan:
    name, kind, attributes = _name_kind_attributes(step, include_content)
    attributes["pitstop.step.kind"] = step.kind
    attributes["pitstop.step.id"] = step.id
    if include_content and step.summary:
        attributes["pitstop.summary"] = step.summary

    status = Status(StatusCode.UNSET)
    if step.status == "error":
        attributes["error.type"] = f"pitstop.{step.kind}_error"
        description = step.error_message if include_content and step.error_message else None
        status = Status(StatusCode.ERROR, description)
    elif step.status == "skipped":
        attributes["pitstop.step.status"] = "skipped"

    end = step.ended_at
    if end is None:
        attributes["pitstop.step.unfinished"] = True
        end = step.started_at

    trace_id = trace_id_for(step.run_id)
    flags = TraceFlags(TraceFlags.SAMPLED)
    parent = (
        None
        if step.parent_id is None
        else SpanContext(trace_id, span_id_for(step.parent_id), is_remote=False, trace_flags=flags)
    )
    return ReadableSpan(
        name=name,
        context=SpanContext(trace_id, span_id_for(step.id), is_remote=False, trace_flags=flags),
        parent=parent,
        resource=resource,
        attributes=attributes,
        kind=kind,
        status=status,
        start_time=to_unix_nanos(step.started_at),
        end_time=to_unix_nanos(end),
        instrumentation_scope=SCOPE,
    )


def _name_kind_attributes(
    step: Step, include_content: bool
) -> tuple[str, SpanKind, dict[str, AttributeValue]]:
    meta = step.meta
    attrs: dict[str, AttributeValue] = {}

    if step.kind == "run":
        run_type = str(meta.get("run_type") or step.name.removesuffix(" run"))
        attrs["pitstop.run.id"] = step.run_id
        attrs["pitstop.run.type"] = run_type
        if step.cost_usd is not None:
            # The run's total, kept apart from agentobs.cost.usd so that
            # summing a trace's costs never counts it twice (ADR-004).
            attrs["pitstop.run.recorded_cost_usd"] = float(step.cost_usd)
        attrs.update(_meta_attributes(meta, include_content))
        if run_type == "backup":
            return "run backup", SpanKind.INTERNAL, attrs
        attrs["gen_ai.operation.name"] = "invoke_workflow"
        attrs["gen_ai.workflow.name"] = run_type
        return f"invoke_workflow {run_type}", SpanKind.INTERNAL, attrs

    if step.kind == "agent":
        attrs["gen_ai.operation.name"] = "invoke_agent"
        attrs["gen_ai.agent.name"] = step.name
        if isinstance(meta.get("agent_type"), str):
            attrs["pitstop.agent.type"] = meta["agent_type"]
        attrs.update(_meta_attributes(meta, include_content, skip=frozenset({"agent_type"})))
        return f"invoke_agent {step.name}", SpanKind.INTERNAL, attrs

    if step.kind == "critic_review":
        # Recorded as e.g. "Ciri reviews analysis (pass 1)".
        critic = f"{step.name.split(' reviews ', 1)[0]} (critic)"
        attrs["gen_ai.operation.name"] = "invoke_agent"
        attrs["gen_ai.agent.name"] = critic
        attrs["pitstop.step.name"] = step.name
        for key, attr in _CRITIC_KEYS.items():
            if _is_scalar(meta.get(key)):
                attrs[attr] = meta[key]
        attrs.update(_meta_attributes(meta, include_content, skip=frozenset(_CRITIC_KEYS)))
        return f"invoke_agent {critic}", SpanKind.INTERNAL, attrs

    if step.kind == "llm_call":
        attrs.update(_llm_attributes(step))
        if include_content:
            attrs.update(_meta_attributes(meta, True, skip=_LLM_KEYS))
        return (f"chat {step.model}" if step.model else "chat"), SpanKind.CLIENT, attrs

    # context, decision, revision, email, error: kept under their own names.
    attrs.update(_meta_attributes(meta, include_content))
    return step.name, SpanKind.INTERNAL, attrs


def _llm_attributes(step: Step) -> dict[str, AttributeValue]:
    meta = step.meta
    attrs: dict[str, AttributeValue] = {
        "gen_ai.operation.name": "chat",
        # Pitstop called models through an OpenAI-compatible API.
        "gen_ai.provider.name": "openai",
    }
    if step.model:
        attrs["gen_ai.request.model"] = step.model
    if step.tokens_in is not None:
        attrs["gen_ai.usage.input_tokens"] = step.tokens_in
    if step.tokens_out is not None:
        attrs["gen_ai.usage.output_tokens"] = step.tokens_out
    if step.cost_usd is not None:
        attrs["agentobs.cost.usd"] = float(step.cost_usd)
    for key, attr in (
        ("cached_tokens", "gen_ai.usage.cache_read.input_tokens"),
        ("reasoning_tokens", "gen_ai.usage.reasoning.output_tokens"),
        ("max_completion_tokens", "gen_ai.request.max_tokens"),
        ("reasoning_effort", "gen_ai.request.reasoning.level"),
        ("attempt", "pitstop.llm.attempt"),
    ):
        if _is_scalar(meta.get(key)):
            attrs[attr] = meta[key]
    if isinstance(meta.get("finish_reason"), str):
        attrs["gen_ai.response.finish_reasons"] = (meta["finish_reason"],)
    if isinstance(meta.get("json"), bool):
        attrs["gen_ai.output.type"] = "json" if meta["json"] else "text"
    return attrs


def _is_scalar(value: Any) -> bool:
    return isinstance(value, bool | int | float | str)


def _meta_attributes(
    meta: Mapping[str, Any], include_content: bool, skip: frozenset[str] = frozenset()
) -> dict[str, AttributeValue]:
    """Numbers, booleans and label strings as pitstop.meta.<key>; other
    strings only with content capture; lists and objects never."""
    attrs: dict[str, AttributeValue] = {}
    for key, value in meta.items():
        if key in skip:
            continue
        recordable = isinstance(value, bool | int | float) or (
            isinstance(value, str) and (key in LABEL_KEYS or include_content)
        )
        if recordable:
            attrs[f"pitstop.meta.{key}"] = value
    return attrs
