"""The end-to-end check (Phase 1, metric 1) on synthetic traces: the
whole path passes, and each way a trace can break is named."""

from dataclasses import replace

from e2e.check import check_trace
from replay.common import StoredSpan, parse_otlp_json

PATH = ("agent-eval-harness", "mcp-security-gateway", "mcp-backbone")


def _span(sid: int, parent: int | None, service: str, name: str) -> StoredSpan:
    return StoredSpan(sid, parent, name, sid * 10, sid * 10 + 5, {}, service, trace_id=7)


def _good() -> list[StoredSpan]:
    """agent turn -> MCP client -> gateway server -> gateway stages ->
    gateway's upstream client -> MCP-backbone server -> its stages."""
    return [
        _span(1, None, "agent-eval-harness", "eval sample e01"),
        _span(2, 1, "agent-eval-harness", "MCP send tools/call run_query"),
        _span(3, 2, "mcp-security-gateway", "tools/call run_query"),
        _span(4, 3, "mcp-security-gateway", "policy"),
        _span(5, 3, "mcp-security-gateway", "MCP send tools/call run_query"),
        _span(6, 5, "mcp-backbone", "tools/call run_query"),
        _span(7, 6, "mcp-backbone", "authenticate"),
        _span(8, 6, "mcp-backbone", "execute"),
        _span(9, 6, "mcp-backbone", "audit"),
        _span(10, 3, "mcp-security-gateway", "detect"),
    ]


def test_the_whole_path_passes() -> None:
    assert check_trace(_good(), PATH) == []


def test_a_hop_that_loses_the_trace_context_is_named() -> None:
    # MCP-backbone's server span points at a parent the trace doesn't have:
    # what a gateway that dropped `_meta` looks like from the viewer.
    spans = [replace(s, parent_id=999) if s.span_id == 6 else s for s in _good()]
    problems = check_trace(spans, PATH)
    assert problems[0] == (
        "mcp-backbone span 'tools/call run_query' has a parent that isn't in the trace "
        "(trace context lost at a hop)"
    )
    assert "no database stage is reached through" in problems[-1]


def test_a_split_trace_is_named() -> None:
    spans = [replace(s, parent_id=None) if s.span_id == 6 else s for s in _good()]
    problems = check_trace(spans, PATH)
    assert problems[0] == "2 root spans, not 1: the trace is split"


def test_a_skipped_gateway_is_caught() -> None:
    # The server span hangs straight off the agent: the request went
    # around the gateway, or the gateway made no spans of its own.
    spans = [
        replace(s, parent_id=2) if s.span_id == 6 else s
        for s in _good()
        if s.service != "mcp-security-gateway"
    ]
    problems = check_trace(spans, PATH)
    assert problems == [
        "services missing from the trace: mcp-security-gateway",
        (
            "no database stage is reached through agent-eval-harness -> mcp-security-gateway -> "
            "mcp-backbone; paths found: agent-eval-harness -> mcp-backbone"
        ),
    ]


def test_a_trace_that_never_reaches_the_database_fails() -> None:
    spans = [s for s in _good() if s.name not in {"execute", "audit"}]
    assert check_trace(spans, PATH) == [
        "no database stage (audit, cost_check, execute) from mcp-backbone"
    ]
    assert check_trace([], PATH) == ["the trace is empty or wasn't found"]


def test_the_path_can_be_given() -> None:
    two = [s for s in _good() if s.service != "mcp-security-gateway"]
    two = [replace(s, parent_id=2) if s.span_id == 6 else s for s in two]
    assert check_trace(two, ("agent-eval-harness", "mcp-backbone")) == []


def test_the_viewer_response_carries_service_and_trace() -> None:
    body = {
        "result": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "gw"}}]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "0000000000000000000000000000000a",
                                    "spanId": "00000000000000ff",
                                    "name": "x",
                                    "startTimeUnixNano": "1",
                                    "endTimeUnixNano": "2",
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }
    (span,) = parse_otlp_json(body)
    assert (span.service, span.trace_id, span.parent_id) == ("gw", 10, None)


def test_every_hop_must_be_the_next_one_on_the_path() -> None:
    # Another service between the gateway and MCP-backbone: the request
    # doesn't go straight from one to the next, so the given path fails,
    # and passes once that service is named in it.
    spans = [replace(s, parent_id=11) if s.span_id == 6 else s for s in _good()]
    spans.append(_span(11, 5, "other-proxy", "tools/call run_query"))
    assert check_trace(spans, PATH)[-1].endswith(
        "paths found: agent-eval-harness -> mcp-security-gateway -> other-proxy -> mcp-backbone"
    )
    assert check_trace(spans, (*PATH[:2], "other-proxy", PATH[2])) == []
    # The right services in the wrong order fail too.
    backwards = ("mcp-security-gateway", "agent-eval-harness", "mcp-backbone")
    assert check_trace(_good(), backwards)[-1].startswith("no database stage is reached through")


def test_only_the_mcp_servers_stages_count_as_reaching_the_database() -> None:
    # The gateway writes its own audit log; a gateway span named "audit"
    # must not stand in for MCP-backbone reaching Postgres.
    spans = [s for s in _good() if s.service != "mcp-backbone"]
    spans.append(_span(12, 3, "mcp-security-gateway", "audit"))
    assert check_trace(spans, PATH)[-1] == (
        "no database stage (audit, cost_check, execute) from mcp-backbone"
    )
