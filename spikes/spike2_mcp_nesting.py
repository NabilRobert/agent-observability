"""Spike 2: do spans opened inside an MCPServer tool nest under the SDK's
`tools/call` span, and is a client span's context carried in `_meta`?
Run with MCP-backbone's environment (mcp 2.3.0). Throwaway."""

import asyncio

from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("spike2")

server = MCPServer("spike2")


@server.tool()
async def probe() -> str:
    with tracer.start_as_current_span("validate"):
        await asyncio.sleep(0)
    return "ok"


async def main() -> None:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        lowlevel = server._lowlevel_server  # pyright: ignore[reportPrivateUsage]
        task = asyncio.ensure_future(
            lowlevel.run(
                *server_streams, lowlevel.create_initialization_options(), raise_exceptions=False
            )
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                with tracer.start_as_current_span("agent turn"):
                    await session.call_tool("probe", {})
        finally:
            task.cancel()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    for s in spans.values():
        parent = next(
            (p.name for p in spans.values() if s.parent and p.context.span_id == s.parent.span_id),
            None,
        )
        print(
            f"{s.name!r:32} kind={s.kind.name:8} trace={s.context.trace_id:032x} parent={parent!r} remote={s.parent.is_remote if s.parent else None}"
        )
    agent, client, srv, stage = (
        spans.get(n)
        for n in ("agent turn", "MCP send tools/call probe", "tools/call probe", "validate")
    )
    ok = bool(agent and client and srv and stage)
    ok = ok and len({s.context.trace_id for s in (agent, client, srv, stage)}) == 1
    ok = (
        ok
        and stage.parent.span_id == srv.context.span_id
        and srv.parent.span_id == client.context.span_id
    )
    print("PASS" if ok else "FAIL")


asyncio.run(main())
