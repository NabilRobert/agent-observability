"""The Pitstop replay's mapping (docs/conventions.md) and its checks, on a
synthetic fixture: two runs shaped like the real records, with a marker
string planted in every field that counts as content."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

from replay.common import StoredSpan, parse_otlp_json
from replay.pitstop.mapping import (
    ReplayError,
    Step,
    group_runs,
    resource_for,
    span_for,
    span_id_for,
    trace_id_for,
)
from replay.pitstop.replay import check, check_run, load_steps

FIXTURE = Path(__file__).parent / "fixtures" / "pitstop_steps.json"
MARKER = "zzcontent9b1"
RESOURCE = resource_for("synthetic.dump")


@pytest.fixture
def steps() -> list[Step]:
    return load_steps(FIXTURE)


def _spans(steps: list[Step], include_content: bool = False) -> dict[str, ReadableSpan]:
    """Spans keyed by recorded step name."""
    return {s.name: span_for(s, resource=RESOURCE, include_content=include_content) for s in steps}


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _ids(span: ReadableSpan) -> tuple[int, int, int | None]:
    assert span.context is not None
    return span.context.trace_id, span.context.span_id, span.parent and span.parent.span_id


def _stored(span: ReadableSpan) -> StoredSpan:
    """What a faithful viewer would hand back."""
    _, span_id, parent = _ids(span)
    assert span.start_time is not None and span.end_time is not None
    return StoredSpan(span_id, parent, span.name, span.start_time, span.end_time, _attrs(span))


def test_each_run_is_one_trace_with_the_recorded_tree(steps: list[Step]) -> None:
    runs = group_runs(steps)
    assert sorted(runs) == [7, 8]
    for run_id, run in runs.items():
        spans = [span_for(s, resource=RESOURCE) for s in run]
        assert {_ids(s)[0] for s in spans} == {trace_id_for(run_id)}
        for step, span in zip(run, spans, strict=True):
            parent = None if step.parent_id is None else span_id_for(step.parent_id)
            assert _ids(span)[1:] == (span_id_for(step.id), parent)
    assert trace_id_for(7) != trace_id_for(8)
    # Pinned, so a replay next month (or in another process) lands on the
    # same trace and span IDs as this one.
    assert f"{trace_id_for(7):032x}" == "7b72089bbaa2b8d832721d1589da2433"
    assert f"{span_id_for(101):016x}" == "4317cf9983f800a3"
    # Deterministic: the same snapshot always gives the same IDs.
    assert [_ids(span_for(s, resource=RESOURCE)) for s in steps] == [
        _ids(span_for(s, resource=RESOURCE)) for s in load_steps(FIXTURE)
    ]


def test_names_and_kinds_follow_the_conventions(steps: list[Step]) -> None:
    spans = _spans(steps)
    assert {name: (s.name, s.kind) for name, s in spans.items()} == {
        "daily run": ("invoke_workflow daily", SpanKind.INTERNAL),
        "Build analysis context": ("Build analysis context", SpanKind.INTERNAL),
        "Anna (analysis)": ("invoke_agent Anna (analysis)", SpanKind.INTERNAL),
        "gpt-5-mini": ("chat gpt-5-mini", SpanKind.CLIENT),
        "Ciri reviews analysis (pass 1)": ("invoke_agent Ciri (critic)", SpanKind.INTERNAL),
        "gpt-5.4": ("chat gpt-5.4", SpanKind.CLIENT),
        "Approve": ("Approve", SpanKind.INTERNAL),
        "Send daily email": ("Send daily email", SpanKind.INTERNAL),
        "backup run": ("run backup", SpanKind.INTERNAL),
        "Skipped step": ("Skipped step", SpanKind.INTERNAL),
    }
    assert dict(RESOURCE.attributes)["service.name"] == "pitstop"
    assert dict(RESOURCE.attributes)["agentobs.replay"] is True


def test_a_model_call_carries_the_genai_attributes(steps: list[Step]) -> None:
    assert _attrs(_spans(steps)["gpt-5-mini"]) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5-mini",
        "gen_ai.usage.input_tokens": 4000,
        "gen_ai.usage.output_tokens": 900,
        "gen_ai.usage.cache_read.input_tokens": 1024,
        "gen_ai.usage.reasoning.output_tokens": 320,
        "gen_ai.request.max_tokens": 8000,
        "gen_ai.request.reasoning.level": "low",
        "gen_ai.response.finish_reasons": ("stop",),
        "gen_ai.output.type": "json",
        "agentobs.cost.usd": 0.002,
        "pitstop.llm.attempt": 1,
        "pitstop.step.kind": "llm_call",
        "pitstop.step.id": 104,
    }
    critic_call = _attrs(_spans(steps)["gpt-5.4"])
    assert critic_call["gen_ai.output.type"] == "text"
    assert "gen_ai.request.reasoning.level" not in critic_call  # recorded as null


def test_agents_critics_and_runs(steps: list[Step]) -> None:
    spans = _spans(steps)
    agent = _attrs(spans["Anna (analysis)"])
    assert agent["gen_ai.agent.name"] == "Anna (analysis)"
    assert agent["pitstop.agent.type"] == "analysis"
    assert agent["pitstop.meta.final_status"] == "approved"
    critic = _attrs(spans["Ciri reviews analysis (pass 1)"])
    assert critic["gen_ai.agent.name"] == "Ciri (critic)"
    assert critic["pitstop.step.name"] == "Ciri reviews analysis (pass 1)"
    assert (critic["pitstop.critic.pass"], critic["pitstop.critic.grade"]) == (1, "B")
    assert critic["pitstop.critic.reviewed_agent"] == "analysis"
    run = _attrs(spans["daily run"])
    assert run["gen_ai.workflow.name"] == "daily"
    assert (run["pitstop.run.id"], run["pitstop.run.type"]) == (7, "daily")
    assert run["pitstop.run.recorded_cost_usd"] == 0.0035
    assert "gen_ai.operation.name" not in _attrs(spans["backup run"])


def test_cost_sits_only_on_the_model_calls(steps: list[Step]) -> None:
    costed = [name for name, s in _spans(steps).items() if "agentobs.cost.usd" in _attrs(s)]
    assert costed == ["gpt-5-mini", "gpt-5.4"]


def test_times_are_the_recorded_times_to_the_microsecond(steps: list[Step]) -> None:
    spans = _spans(steps)
    run, email = spans["daily run"], spans["Send daily email"]
    # 2026-09-30T00:00:00.000100Z
    assert run.start_time == 1_790_726_400_000_100_000
    assert run.end_time == 1_790_726_420_000_000_000
    # The recorded quirk is kept: the email ends after its run does.
    assert email.end_time is not None and run.end_time is not None
    assert email.end_time - run.end_time == 480_000_000


def test_errors_skips_and_unfinished_steps(steps: list[Step]) -> None:
    spans = _spans(steps)
    approve = spans["Approve"]
    assert approve.status.status_code is StatusCode.ERROR
    assert approve.status.description is None
    assert _attrs(approve)["error.type"] == "pitstop.decision_error"
    skipped = _attrs(spans["Skipped step"])
    assert skipped["pitstop.step.status"] == "skipped"
    assert skipped["pitstop.step.unfinished"] is True
    assert spans["Skipped step"].end_time == spans["Skipped step"].start_time


def _all_text(span: ReadableSpan) -> str:
    return "\n".join(
        [span.name, span.status.description or ""] + [f"{k}={v}" for k, v in _attrs(span).items()]
    )


def test_no_content_by_default_and_all_of_it_with_capture(steps: list[Step]) -> None:
    assert [n for n, s in _spans(steps).items() if MARKER in _all_text(s)] == []
    with_content = _spans(steps, include_content=True)
    leaked = {n for n, s in with_content.items() if MARKER in _all_text(s)}
    # Summaries, free-text meta and error messages; lists and objects never.
    assert leaked == {
        "Build analysis context",
        "Anna (analysis)",
        "gpt-5-mini",
        "Ciri reviews analysis (pass 1)",
        "Approve",
        "Send daily email",
    }
    assert "pitstop.meta.issues" not in _attrs(with_content["Ciri reviews analysis (pass 1)"])
    assert "pitstop.meta.rows" not in _attrs(with_content["Build analysis context"])


def test_a_run_that_isnt_one_tree_is_refused(steps: list[Step]) -> None:
    two_roots = [*steps, replace(steps[1], id=999, parent_id=None)]
    with pytest.raises(ReplayError, match="run 7 has 2 roots"):
        group_runs(two_roots)
    orphan = [*steps, replace(steps[1], id=998, parent_id=201)]
    with pytest.raises(ReplayError, match=r"run 7: steps \[998\] have a parent outside"):
        group_runs(orphan)


def _faithful_viewer(steps: list[Step]) -> dict[int, list[StoredSpan]]:
    store: dict[int, list[StoredSpan]] = {}
    for s in steps:
        span = span_for(s, resource=RESOURCE)
        store.setdefault(_ids(span)[0], []).append(_stored(span))
    return store


def test_the_checks_pass_on_a_faithful_copy(steps: list[Step]) -> None:
    store = _faithful_viewer(steps)
    problems, runs, costed = check(steps, lambda trace_id: store.get(trace_id, []))
    assert (problems, runs, costed) == ([], 2, 1)


def test_the_checks_name_every_kind_of_difference(steps: list[Step]) -> None:
    run = group_runs(steps)[7]
    good = _faithful_viewer(steps)[trace_id_for(7)]
    assert check_run(run, good) == []

    missing = good[:-1]
    assert check_run(run, missing) == [
        "run 7: 7 spans stored, 8 steps recorded",
        "run 7: step 108 stored 0 times",
    ]
    doubled = [*good, good[-1]]
    assert "run 7: step 108 stored 2 times" in check_run(run, doubled)

    shifted = [
        replace(s, start=s.start + 1000, end=s.end + 1000) if s.name == "Approve" else s
        for s in good
    ]
    assert check_run(run, shifted) == ["run 7: step 107 starts at a different time"]
    longer = [replace(s, end=s.end + 2_000_000) if s.name == "Approve" else s for s in good]
    assert check_run(run, longer) == ["run 7: step 107 lasts a different time"]
    reparented = [
        replace(s, parent_id=span_id_for(101)) if s.name == "Approve" else s for s in good
    ]
    assert check_run(run, reparented) == ["run 7: step 107 has the wrong parent"]

    dearer = [
        replace(s, attributes={**s.attributes, "agentobs.cost.usd": 0.0021})
        if s.name == "chat gpt-5-mini"
        else s
        for s in good
    ]
    assert check_run(run, dearer) == ["run 7: model-call costs add up to 0.0036, recorded 0.0035"]


def test_viewer_responses_are_parsed_from_otlp_json() -> None:
    body = json.loads(
        """{"result": {"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [
          {"traceId": "00", "spanId": "00000000000000ff", "parentSpanId": "",
           "name": "root", "startTimeUnixNano": "5", "endTimeUnixNano": "9",
           "attributes": [{"key": "n", "value": {"intValue": "42"}},
                          {"key": "c", "value": {"doubleValue": 0.5}},
                          {"key": "s", "value": {"stringValue": "x"}},
                          {"key": "b", "value": {"boolValue": true}}]},
          {"traceId": "00", "spanId": "0000000000000100", "parentSpanId": "00000000000000ff",
           "name": "child", "startTimeUnixNano": "6", "endTimeUnixNano": "7"}]}]}]}}"""
    )
    root, child = parse_otlp_json(body)
    assert root == StoredSpan(255, None, "root", 5, 9, {"n": 42, "c": 0.5, "s": "x", "b": True})
    assert (child.span_id, child.parent_id, child.attributes) == (256, 255, {})
