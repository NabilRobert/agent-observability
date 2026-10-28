"""The Inspect replay's mapping (docs/conventions.md) and its checks, on a
synthetic .eval log shaped like the harness's: one sample with Inspect's
span tree, two model calls (one cancelled), two tool calls (one failed),
a score and a message limit, and a marker string in every content field.
Written with zstd, as Inspect writes them."""

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

from replay.common import StoredSpan
from replay.inspect.mapping import Sample, read_eval, resource_for, spans_for, trace_id_for
from replay.inspect.replay import check, check_sample

MARKER = "zzcontent4c2"
RESOURCE = resource_for("runs/synthetic")
T = "2026-10-24T07:42:"


def _span(sid: str, parent: str | None, kind: str, name: str, start: str, end: str) -> list[Any]:
    begin: dict[str, Any] = {
        "event": "span_begin",
        "id": sid,
        "type": kind,
        "name": name,
        "timestamp": T + start,
    }
    if parent:
        begin["parent_id"] = parent
    return [begin, {"event": "span_end", "id": sid, "timestamp": T + end}]


def _usage(i: int, o: int, cost: float) -> dict[str, Any]:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "input_tokens_cache_read": 0,
        "reasoning_tokens": 64,
        "total_cost": cost,
    }


SAMPLE: dict[str, Any] = {
    "id": "e01",
    "epoch": 1,
    "uuid": "sample-uuid-1",
    "started_at": T + "00.000100+00:00",
    "completed_at": T + "09.000000+00:00",
    "model_usage": {
        "openai-api/sumopod/gpt-5-mini": {
            "input_tokens": 1500,
            "output_tokens": 300,
            "total_cost": 0.0015,
        }
    },
    "scores": {"suite_scorer": {"value": {"strict": 1.0, "lenient": 1.0}}},
    "limit": {"type": "message", "limit": 20.0},
    "events": [
        *_span("S", None, "solvers", "solvers", "00.000200+00:00", "08.900000+00:00"),
        *_span("V", "S", "solver", "react", "00.000300+00:00", "08.800000+00:00"),
        *_span("A", "V", "agent", "react", "00.000400+00:00", "08.700000+00:00"),
        {
            "event": "model",
            "uuid": "m1",
            "span_id": "A",
            "model": "openai-api/sumopod/gpt-5-mini",
            "timestamp": T + "00.100000+00:00",
            "completed": T + "03.250000+00:00",
            "input": [{"content": MARKER}],
            "output": {
                "usage": _usage(1000, 200, 0.001),
                "choices": [{"stop_reason": "tool_calls", "message": {"content": MARKER}}],
                "response_id": "resp_1",
            },
        },
        *_span("T1", "A", "tool", "run_query", "03.300000+00:00", "03.360000+00:00"),
        {
            "event": "tool",
            "id": "call_1",
            "span_id": "A",
            "function": "run_query",
            "timestamp": T + "03.300100+00:00",
            "arguments": {"sql": f"select '{MARKER}'"},
            "result": MARKER,
        },
        *_span("T2", "A", "tool", "sample_rows", "03.400000+00:00", "03.450000+00:00"),
        {
            "event": "tool",
            "id": "call_2",
            "span_id": "A",
            "function": "sample_rows",
            "timestamp": T + "03.400100+00:00",
            "arguments": {"table_name": MARKER},
            "error": {"type": "unknown", "message": f"table '{MARKER}' not found"},
        },
        {
            "event": "model",
            "uuid": "m2",
            "span_id": "A",
            "model": "openai-api/sumopod/gpt-5-mini",
            "timestamp": T + "03.500000+00:00",
            "error": f"RuntimeError('cancelled {MARKER}')",
            "output": {"usage": _usage(500, 100, 0.0005)},
        },
        *_span("C", None, "scorers", "scorers", "08.950000+00:00", "08.990000+00:00"),
        {"event": "score", "score": {"value": {"strict": 1.0}}},
    ],
}


def _write_eval(path: Path, samples: list[dict[str, Any]]) -> Path:
    header = {
        "eval": {
            "eval_id": "eval-1",
            "task": "task",
            "model": "openai-api/sumopod/gpt-5-mini",
        }
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_ZSTANDARD) as z:
        z.writestr("header.json", json.dumps(header))
        for s in samples:
            z.writestr(f"samples/{s['id']}_epoch_{s['epoch']}.json", json.dumps(s))
    return path


@pytest.fixture
def sample(tmp_path: Path) -> Sample:
    (s,) = read_eval(_write_eval(tmp_path / "synthetic.eval", [SAMPLE]))
    return s


def _by_name(spans: list[ReadableSpan]) -> dict[str, ReadableSpan]:
    return {
        s.name + ("#2" if s.attributes and s.attributes.get("inspect.event.unfinished") else ""): s
        for s in spans
    }


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _parent_name(spans: list[ReadableSpan], span: ReadableSpan) -> str | None:
    if span.parent is None:
        return None
    return next(
        s.name for s in spans if s.context is not None and s.context.span_id == span.parent.span_id
    )


def test_one_trace_with_inspects_own_tree(sample: Sample) -> None:
    spans = spans_for(sample, resource=RESOURCE)
    assert sample.run == "synthetic"
    assert {s.context.trace_id for s in spans if s.context} == {trace_id_for(sample)}
    assert [(s.name, _parent_name(spans, s), s.kind) for s in spans] == [
        ("eval sample e01", None, SpanKind.INTERNAL),
        ("solvers", "eval sample e01", SpanKind.INTERNAL),
        ("solver react", "solvers", SpanKind.INTERNAL),
        ("invoke_agent react", "solver react", SpanKind.INTERNAL),
        ("execute_tool run_query", "invoke_agent react", SpanKind.INTERNAL),
        ("execute_tool sample_rows", "invoke_agent react", SpanKind.INTERNAL),
        ("scorers", "eval sample e01", SpanKind.INTERNAL),
        ("chat gpt-5-mini", "invoke_agent react", SpanKind.CLIENT),
        ("chat gpt-5-mini", "invoke_agent react", SpanKind.CLIENT),
    ]
    # Pinned, so a replay next month lands on the same trace.
    assert f"{trace_id_for(sample):032x}" == "d5abea740af0b00164a325c59e291fb2"


def test_a_model_call_carries_the_genai_attributes_and_its_own_cost(sample: Sample) -> None:
    first = _by_name(spans_for(sample, resource=RESOURCE))["chat gpt-5-mini"]
    assert _attrs(first) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5-mini",
        "inspect.model": "openai-api/sumopod/gpt-5-mini",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 200,
        "gen_ai.usage.cache_read.input_tokens": 0,
        "gen_ai.usage.reasoning.output_tokens": 64,
        "agentobs.cost.usd": 0.001,
        "gen_ai.response.finish_reasons": ("tool_calls",),
        "gen_ai.response.id": "resp_1",
    }
    # 07:42:00.100000 to 07:42:03.250000, to the microsecond.
    assert first.start_time == 1_792_827_720_100_000_000
    assert first.end_time == 1_792_827_723_250_000_000


def test_a_cancelled_model_call_and_a_failed_tool_call(sample: Sample) -> None:
    spans = _by_name(spans_for(sample, resource=RESOURCE))
    cancelled = spans["chat gpt-5-mini#2"]
    assert cancelled.end_time == cancelled.start_time
    assert cancelled.status.status_code is StatusCode.ERROR
    assert cancelled.status.description is None
    assert _attrs(cancelled)["error.type"] == "RuntimeError"
    failed = spans["execute_tool sample_rows"]
    assert failed.status.status_code is StatusCode.ERROR
    assert _attrs(failed)["error.type"] == "inspect.tool_unknown"
    assert _attrs(failed)["gen_ai.tool.call.id"] == "call_2"
    ok = spans["execute_tool run_query"]
    assert ok.status.status_code is StatusCode.UNSET
    assert _attrs(ok)["gen_ai.tool.call.id"] == "call_1"


def test_the_sample_span_carries_the_recorded_totals_score_and_limit(sample: Sample) -> None:
    root = _attrs(spans_for(sample, resource=RESOURCE)[0])
    assert root["inspect.recorded.cost_usd"] == 0.0015
    assert (root["inspect.recorded.input_tokens"], root["inspect.recorded.output_tokens"]) == (
        1500,
        300,
    )
    assert root["inspect.score.suite_scorer.strict"] == 1.0
    assert (root["inspect.limit.type"], root["inspect.limit.value"]) == ("message", 20.0)
    assert "agentobs.cost.usd" not in root  # cost only on the calls that spent it


def _all_text(span: ReadableSpan) -> str:
    return "\n".join(
        [span.name, span.status.description or ""] + [f"{k}={v}" for k, v in _attrs(span).items()]
    )


def test_no_content_by_default_and_arguments_and_errors_with_capture(sample: Sample) -> None:
    assert [s.name for s in spans_for(sample, resource=RESOURCE) if MARKER in _all_text(s)] == []
    leaked = [
        s.name
        for s in spans_for(sample, resource=RESOURCE, include_content=True)
        if MARKER in _all_text(s)
    ]
    # Tool arguments and error messages; never prompts, outputs or results.
    assert sorted(leaked) == [
        "chat gpt-5-mini",
        "execute_tool run_query",
        "execute_tool sample_rows",
    ]


def _stored(span: ReadableSpan) -> StoredSpan:
    assert span.context is not None and span.start_time is not None and span.end_time is not None
    parent = span.parent.span_id if span.parent else None
    return StoredSpan(
        span.context.span_id, parent, span.name, span.start_time, span.end_time, _attrs(span)
    )


def test_the_checks_pass_on_a_faithful_copy_and_name_each_difference(sample: Sample) -> None:
    good = [_stored(s) for s in spans_for(sample, resource=RESOURCE)]
    assert check([sample], lambda _: good) == []
    label = "synthetic sample e01 epoch 1"

    assert check_sample(sample, good[:-1]) == [
        f"{label}: 8 spans stored, 9 sent",
        f"{label}: span 'chat gpt-5-mini' stored 0 times",
        f"{label}: model calls cost 0.001, recorded 0.0015",
        f"{label}: model calls used 1000 input tokens, recorded 1500",
        f"{label}: model calls used 200 output tokens, recorded 300",
    ]
    shifted = [replace(s, start=s.start + 1) if s.name == "scorers" else s for s in good]
    assert check_sample(sample, shifted) == [f"{label}: span 'scorers' has different times"]
    # Jaeger stores the cancelled call's zero-length span as 1 ns long:
    # accepted. A real 1 ns difference anywhere else is not.
    as_jaeger = [replace(s, end=s.end + 1) if s.start == s.end else s for s in good]
    assert check_sample(sample, as_jaeger) == []
    longer = [replace(s, end=s.end + 1) if s.name == "scorers" else s for s in good]
    assert check_sample(sample, longer) == [f"{label}: span 'scorers' has different times"]
    dearer = [
        replace(s, attributes={**s.attributes, "agentobs.cost.usd": 0.002})
        if s.attributes.get("gen_ai.response.id") == "resp_1"
        else s
        for s in good
    ]
    assert check_sample(sample, dearer) == [f"{label}: model calls cost 0.0025, recorded 0.0015"]
