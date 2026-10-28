"""Smoke test for the viewer: sends one span over OTLP/HTTP and reads it
back from Jaeger's query API. Exits 0 when the round trip works.

    uv run python viewer/smoke.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
QUERY = os.environ.get("JAEGER_QUERY_URL", "http://127.0.0.1:16686")


def main() -> int:
    provider = TracerProvider(resource=Resource.create({"service.name": "viewer-smoke"}))
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP}/v1/traces", timeout=5))
    )
    with provider.get_tracer("smoke").start_as_current_span("smoke") as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"
    provider.shutdown()

    for _ in range(20):
        try:
            with urllib.request.urlopen(f"{QUERY}/api/v3/traces/{trace_id}", timeout=5) as r:
                body = json.load(r)
            names = [
                s["name"]
                for rs in body["result"]["resourceSpans"]
                for ss in rs["scopeSpans"]
                for s in ss["spans"]
            ]
            if names == ["smoke"]:
                print(f"ok: trace {trace_id} stored and read back")
                return 0
        except urllib.error.URLError, KeyError:
            pass
        time.sleep(0.5)
    print(f"failed: trace {trace_id} not readable from {QUERY}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
