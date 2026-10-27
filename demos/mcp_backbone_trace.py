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
import datetime
import os
import sys
from pathlib import Path

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
QUERIES = [
    "select title, rental_rate from film order by rental_rate desc limit 3",
    "delete from film",
]


def _test_database_url(checkout: Path) -> str:
    """APP_TEST_DATABASE_URL, as MCP-backbone's tests/conftest.py finds it:
    from the environment or the checkout's .env. Never printed."""
    url = os.environ.get("APP_TEST_DATABASE_URL")
    env_file = checkout / ".env"
    if not url and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("APP_TEST_DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip("\"'")
    if not url:
        sys.exit("no APP_TEST_DATABASE_URL (environment or the checkout's .env)")
    return url


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
    from cryptography.fernet import Fernet

    os.environ["APP_DATABASE_URL"] = os.environ["APP_TARGET_DATABASE_URL"] = _test_database_url(
        checkout
    )
    os.environ["APP_CONNECTION_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ["APP_ALLOW_PRIVATE_TARGETS"] = "true"

    from app.auth.models import ApiKey, Workspace
    from app.auth.security import generate_api_key, hash_api_key
    from app.db import Base, SessionLocal, dispose_target_engines, engine
    from app.query.models import DataSourceConnection
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from sqlalchemy import text

    if "test" not in (engine.url.database or ""):
        sys.exit("refusing: the database name doesn't contain 'test'")

    provider = TracerProvider(resource=Resource.create({"service.name": "demo-agent"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP}/v1/traces")))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("demo-agent")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with SessionLocal() as session:
            now = datetime.datetime.now(datetime.UTC)
            workspace = Workspace(name="trace-demo", created_at=now)
            session.add(workspace)
            await session.flush()
            session.add(
                DataSourceConnection(
                    workspace_id=workspace.id,
                    connection_url=engine.url.render_as_string(hide_password=False),
                )
            )
            api_key = generate_api_key()
            session.add(
                ApiKey(
                    workspace_id=workspace.id,
                    key_hash=hash_api_key(api_key),
                    status="active",
                    created_at=now,
                )
            )
            await session.execute(
                text("create table film (id serial primary key, title text, rental_rate numeric)")
            )
            await session.execute(
                text(
                    "insert into film (title, rental_rate) values "
                    "('Academy Dinosaur', 0.99), ('Ace Goldfinger', 4.99), "
                    "('Adaptation Holes', 2.99), ('Affair Prejudice', 2.99)"
                )
            )
            await session.commit()

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
        async with engine.begin() as conn:
            await conn.execute(text("drop table if exists film"))
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_target_engines()
        await engine.dispose()
        provider.shutdown()


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        asyncio.run(agent(Path(sys.argv[1]).resolve()))
