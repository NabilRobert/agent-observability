"""Spike 4 (and the data half of Spike 1): build spans with our own IDs,
parent and timestamps through public SDK API only, export them over
OTLP/HTTP, and read them back from Jaeger's query API. Throwaway."""

import json
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags

QUERY = "http://127.0.0.1:16686/api/v3"
RESOURCE = Resource.create({"service.name": "spike4"})


def ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def make(trace_id: int, span_id: int, parent: int | None, name: str, start: datetime, ms: int):
    ctx = SpanContext(
        trace_id, span_id, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED)
    )
    parent_ctx = (
        SpanContext(trace_id, parent, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))
        if parent
        else None
    )
    return ReadableSpan(
        name=name,
        context=ctx,
        parent=parent_ctx,
        resource=RESOURCE,
        attributes={"spike.age": name},
        kind=SpanKind.INTERNAL,
        start_time=ns(start),
        end_time=ns(start + timedelta(milliseconds=ms)),
    )


def main() -> int:
    now = datetime.now(UTC).replace(microsecond=123456)
    cases = {"now": 0, "20d": 20, "40d": 40}
    spans = []
    for i, (label, days) in enumerate(cases.items(), start=1):
        t0 = now - timedelta(days=days)
        tid = 0xABC0000000000000000000000000000 + i
        spans.append(make(tid, 0x1000 + i, None, f"root {label}", t0, 500))
        spans.append(
            make(
                tid, 0x2000 + i, 0x1000 + i, f"child {label}", t0 + timedelta(milliseconds=100), 250
            )
        )
    result = OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces").export(spans)
    print("export:", result)
    if result is not SpanExportResult.SUCCESS:
        return 1
    time.sleep(2)
    ok = True
    for i, label in enumerate(cases, start=1):
        tid = f"{0xABC0000000000000000000000000000 + i:032x}"
        try:
            with urllib.request.urlopen(f"{QUERY}/traces/{tid}") as r:
                body = json.load(r)
        except Exception as exc:  # noqa: BLE001
            print(label, "NOT FOUND", exc)
            ok = False
            continue
        got = [
            (
                s["name"],
                s["spanId"],
                s.get("parentSpanId", ""),
                s["startTimeUnixNano"],
                s["endTimeUnixNano"],
            )
            for rs in body["result"]["resourceSpans"]
            for ss in rs["scopeSpans"]
            for s in ss["spans"]
        ]
        want = sorted(
            (
                s.name,
                f"{s.context.span_id:016x}",
                f"{s.parent.span_id:016x}" if s.parent else "",
                str(s.start_time),
                str(s.end_time),
            )
            for s in spans
            if s.context.trace_id == int(tid, 16)
        )
        same = sorted(got) == want
        ok &= same
        print(label, "found", len(got), "spans; identical IDs/parent/times:", same)
        if not same:
            print("  got ", sorted(got))
            print("  want", want)
    # Search by time range, as the UI does.
    for label, days in cases.items():
        lo = (now - timedelta(days=days, hours=1)).isoformat().replace("+00:00", "Z")
        hi = (now - timedelta(days=days) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        url = (
            f"{QUERY}/traces?query.service_name=spike4&query.start_time_min={lo}"
            f"&query.start_time_max={hi}&query.search_depth=20"
        )
        try:
            with urllib.request.urlopen(url) as r:
                n = sum(
                    len(ss["spans"])
                    for rs in json.load(r)["result"]["resourceSpans"]
                    for ss in rs["scopeSpans"]
                )
        except Exception as exc:  # noqa: BLE001
            n = f"error {exc}"
        print(f"search {label}: {n} spans")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
