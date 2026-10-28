"""What every replay shares: exact timestamps, derived IDs, sending spans
over OTLP, and reading them back from the viewer to check them."""

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
QUERY = os.environ.get("JAEGER_QUERY_URL", "http://127.0.0.1:16686")
BATCH = 256
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def to_unix_nanos(t: datetime) -> int:
    """Exact: whole microseconds, as the records store them, never a float."""
    return (t - _EPOCH) // timedelta(microseconds=1) * 1000


def derived_id(key: str, size: int) -> int:
    """A trace (16-byte) or span (8-byte) ID derived from a key, so the
    same records always replay to the same IDs."""
    value = int.from_bytes(hashlib.sha256(key.encode()).digest()[:size], "big")
    return value or 1  # an all-zero ID is invalid; vanishingly unlikely


def send_spans(spans: Sequence[ReadableSpan]) -> bool:
    """Sends spans to the viewer in batches; False if any batch failed."""
    exporter = OTLPSpanExporter(endpoint=f"{OTLP.rstrip('/')}/v1/traces", timeout=10)
    try:
        for i in range(0, len(spans), BATCH):
            if exporter.export(spans[i : i + BATCH]) is not SpanExportResult.SUCCESS:
                return False
        return True
    finally:
        exporter.shutdown()


@dataclass(frozen=True)
class StoredSpan:
    """One span as the viewer returns it."""

    span_id: int
    parent_id: int | None
    name: str
    start: int
    end: int
    attributes: Mapping[str, Any]
    service: str = ""
    trace_id: int = 0


def parse_otlp_json(body: Mapping[str, Any]) -> list[StoredSpan]:
    """The spans in a Jaeger v3 API trace response (OTLP JSON)."""
    out: list[StoredSpan] = []
    for rs in body["result"]["resourceSpans"]:
        resource = {
            a["key"]: _otlp_value(a["value"]) for a in rs.get("resource", {}).get("attributes", [])
        }
        service = str(resource.get("service.name", ""))
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
                        service=service,
                        trace_id=int(s["traceId"], 16) if s.get("traceId") else 0,
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


def fetch_trace(trace_id: int) -> list[StoredSpan]:
    try:
        with urllib.request.urlopen(f"{QUERY}/api/v3/traces/{trace_id:032x}", timeout=10) as r:
            return parse_otlp_json(json.load(r))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def query_traces(service: str, start: datetime, end: datetime, limit: int = 50) -> list[StoredSpan]:
    """The spans of the traces `service` took part in between start and end."""
    params = urllib.parse.urlencode(
        {
            "query.service_name": service,
            "query.start_time_min": start.isoformat().replace("+00:00", "Z"),
            "query.start_time_max": end.isoformat().replace("+00:00", "Z"),
            "query.search_depth": str(limit),
        }
    )
    try:
        with urllib.request.urlopen(f"{QUERY}/api/v3/traces?{params}", timeout=10) as r:
            return parse_otlp_json(json.load(r))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def compare_spans(
    label: str, expected: Sequence[ReadableSpan], stored: Sequence[StoredSpan]
) -> list[str]:
    """What differs between the spans sent and the viewer's copy: every
    span once, with the same parent, start and end, to the nanosecond."""
    problems: list[str] = []
    if len(stored) != len(expected):
        problems.append(f"{label}: {len(stored)} spans stored, {len(expected)} sent")
    by_id: dict[int, list[StoredSpan]] = {}
    for s in stored:
        by_id.setdefault(s.span_id, []).append(s)
    for span in expected:
        assert span.context is not None
        copies = by_id.get(span.context.span_id, [])
        if len(copies) != 1:
            problems.append(f"{label}: span {span.name!r} stored {len(copies)} times")
            continue
        (got,) = copies
        parent = span.parent.span_id if span.parent else None
        if got.parent_id != parent:
            problems.append(f"{label}: span {span.name!r} has the wrong parent")
        # Jaeger stores a zero-length span (a step with no recorded end) as
        # 1 ns long, treating a zero duration as unset; that is not a
        # difference in the records.
        zero_length = span.start_time == span.end_time and got.end == got.start + 1
        if got.start != span.start_time or (got.end != span.end_time and not zero_length):
            problems.append(f"{label}: span {span.name!r} has different times")
    return problems
