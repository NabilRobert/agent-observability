"""One real trace across two processes, for the viewer: a scripted "agent"
(service `demo-agent`, no model) calls MCP-backbone (service
`mcp-backbone`) over stdio, and both export to the local viewer. The
agent's context crosses the process boundary in each request's `_meta`.

Runs against MCP-backbone's disposable test database (the one its test
suite wipes after every test), seeds a workspace and a small table, makes
two calls (one query that runs, one DELETE that is refused), and drops
everything again.

    uv run --project <MCP-backbone checkout> python demos/mcp_backbone_trace.py <checkout>
"""

import asyncio
import os
import sys
from pathlib import Path

from backbone import seeded, use_test_database

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
QUERIES = [
    "select title, rental_rate from film order by rental_rate desc limit 3",
    "delete from film",
]


def serve() -> None:
    """The MCP-backbone side: the real server, over stdio, with tracing on."""
    from app.mcp.server import mcp
    from app.tracing import configure_tracing

    provider = configure_tracing()
    try:
        mcp.run("stdio")
    finally:
        if provider is not None:
            provider.shutdown()


async def agent(checkout: Path) -> None:
    use_test_database(checkout)

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "demo-agent"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP}/v1/traces")))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("demo-agent")

    try:
        async with seeded() as api_key:
            server = StdioServerParameters(
                command=sys.executable,
                args=[__file__, "serve"],
                env={
                    **os.environ,
                    "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP,
                    "OTEL_SERVICE_NAME": "mcp-backbone",
                },
            )
            async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                with tracer.start_as_current_span(
                    "invoke_agent demo-agent",
                    attributes={
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": "demo-agent",
                    },
                ) as root:
                    for i, sql in enumerate(QUERIES, start=1):
                        with tracer.start_as_current_span(
                            "execute_tool run_query",
                            attributes={
                                "gen_ai.operation.name": "execute_tool",
                                "gen_ai.tool.name": "run_query",
                                "gen_ai.tool.call.id": f"call_{i}",
                            },
                        ):
                            result = await session.call_tool(
                                "run_query", {"sql": sql}, meta={"api_key": api_key}
                            )
                            status = (result.structured_content or {}).get("status")
                            print(f"call {i}: {status}")
                    print(f"trace {root.get_span_context().trace_id:032x}")
    finally:
        provider.shutdown()


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        asyncio.run(agent(Path(sys.argv[1]).resolve()))
