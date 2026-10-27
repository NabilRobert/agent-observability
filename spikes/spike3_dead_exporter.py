"""Spike 3: does an exporter pointed at a closed port delay the traced
work, or a shutdown? Throwaway."""

import statistics
import time

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def run(endpoint: str | None, timeout: float | None) -> None:
    provider = TracerProvider()
    if endpoint:
        kwargs = {"timeout": timeout} if timeout else {}
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, **kwargs)))
    tracer = provider.get_tracer("spike3")
    per_call = []
    for _ in range(50):
        t = time.perf_counter()
        with tracer.start_as_current_span("tool"):
            for stage in ("validate", "cost_check", "execute", "audit"):
                with tracer.start_as_current_span(stage):
                    pass
        per_call.append((time.perf_counter() - t) * 1e6)
    t = time.perf_counter()
    provider.shutdown()
    shut = time.perf_counter() - t
    print(
        f"{endpoint or 'no exporter'} timeout={timeout}: per call p50 {statistics.median(per_call):.0f} µs,"
        f" max {max(per_call):.0f} µs; shutdown {shut:.2f} s"
    )


run(None, None)
run("http://127.0.0.1:4318/v1/traces", None)  # live viewer
run("http://127.0.0.1:9/v1/traces", None)  # closed port, default timeout
run("http://127.0.0.1:9/v1/traces", 2.0)  # closed port, 2 s timeout
