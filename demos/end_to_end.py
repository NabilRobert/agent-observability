"""The end-to-end path, run locally at $0: an agent -> mcp-security-gateway
-> MCP-backbone -> Postgres, every hop exporting to the local viewer, as
one request (ADR-009). Then `uv run python -m e2e.check` checks the trace.

- MCP-backbone: the given checkout, served over HTTP on 127.0.0.1:8766,
  on its disposable test database (seeded, then wiped), traced.
- The gateway: an export of the given ref of its repo (`git archive`, so
  that checkout is never touched), on 127.0.0.1:8080 with a `pagila`
  route, the address the harness's `mcp-backbone-gateway` target uses.
  Rules-only detection: no model is downloaded.
- The agent: a scripted one (no model), until the harness's scripted Pagila
  agent exists; it lists the tables and counts the films.

    uv run --project <MCP-backbone checkout> python demos/end_to_end.py \\
        --backbone <MCP-backbone checkout> --gateway <mcp-gateway repo> [--gateway-ref origin/main]

Working files (the gateway export, its config and logs) go in a temp
folder, agent-observability-e2e. The viewer must be running.
"""

import argparse
import asyncio
import contextlib
import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from backbone import seeded, use_test_database

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
BACKBONE_PORT = 8766  # 8765 is the harness's own (untraced) local copy
GATEWAY_URL = "http://127.0.0.1:8080/mcp/pagila/"
# Outside the repo: uv reads settings from parent folders, and this repo's
# package-age limit would reject the gateway's own locked versions.
WORK = Path(tempfile.gettempdir()) / "agent-observability-e2e"
QUERY = "select count(*) as films from film"

GATEWAY_CONFIG = """\
# Written by agent-observability's demos/end_to_end.py; local only.
upstreams:
  pagila:
    transport: http
    url: http://127.0.0.1:{port}/mcp/
    auth_env: MCP_BACKBONE_LOCAL_KEY
callers:
  e2e:
    key_sha256: "{key_hash}"
    policy: read-only
policies:
  read-only:
    routes:
      pagila:
        tools: {{list_tables: {{}}, describe_table: {{}}, run_query: {{}}}}
detector:
  rules: true
audit:
  path: {audit}
  fsync: false
"""


def export_gateway(repo: Path, ref: str) -> Path:
    """The gateway at `ref`, unpacked from git and synced, cached by commit."""
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=12", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dest = WORK / f"mcp-gateway-{sha}"
    if not (dest / "pyproject.toml").exists():
        dest.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", ref], check=True, capture_output=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    subprocess.run(["uv", "sync", "--locked", "-q"], cwd=dest, check=True)
    print(f"gateway: {ref} ({sha})")
    return dest


def wait_healthy(url: str, what: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    for _ in range(120):
        if proc.poll() is not None:
            sys.exit(f"{what} exited ({proc.returncode}); see {log}")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    sys.exit(f"{what} never became healthy at {url}; see {log}")


@contextlib.contextmanager
def running(
    cmd: list[str], env: dict[str, str], cwd: Path, log: Path
) -> Iterator[subprocess.Popen[bytes]]:
    with log.open("wb") as out:
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
        try:
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


async def agent(caller_key: str) -> None:
    """A scripted agent: its own trace, and two tool calls through the gateway."""
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
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
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {caller_key}"}) as http,
            streamable_http_client(GATEWAY_URL, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            with tracer.start_as_current_span(
                "invoke_agent demo-agent",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "demo-agent",
                },
            ) as root:
                for i, (tool, args) in enumerate(
                    [("list_tables", {}), ("run_query", {"sql": QUERY})], start=1
                ):
                    with tracer.start_as_current_span(
                        f"execute_tool {tool}",
                        attributes={
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": tool,
                            "gen_ai.tool.call.id": f"call_{i}",
                        },
                    ):
                        result = await session.call_tool(tool, args)
                        print(f"{tool}: {'error' if result.is_error else 'ok'}")
                print(f"trace {root.get_span_context().trace_id:032x}")
    finally:
        provider.shutdown()


async def run(backbone: Path, gateway_repo: Path, ref: str) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    gateway = export_gateway(gateway_repo, ref)
    use_test_database(backbone)
    caller_key = secrets.token_urlsafe(32)
    config = WORK / "gateway.yaml"
    config.write_text(
        GATEWAY_CONFIG.format(
            port=BACKBONE_PORT,
            key_hash=hashlib.sha256(caller_key.encode()).hexdigest(),
            audit=WORK / "audit.jsonl",
        )
    )
    config.chmod(0o600)

    async with seeded(query_rate_limit=10**6) as backbone_key:
        traced = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP}
        backbone_cmd = [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(BACKBONE_PORT),
        ]  # fmt: skip
        gateway_env = {
            **traced,
            "OTEL_SERVICE_NAME": "mcp-security-gateway",
            "MCP_BACKBONE_LOCAL_KEY": backbone_key,
        }
        gateway_env.pop("VIRTUAL_ENV", None)  # the gateway's own environment, not this one
        with running(
            backbone_cmd,
            {**traced, "OTEL_SERVICE_NAME": "mcp-backbone"},
            WORK,  # not the checkout, so its .env isn't read
            WORK / "mcp-backbone.log",
        ) as backbone_proc:
            wait_healthy(
                f"http://127.0.0.1:{BACKBONE_PORT}/healthz",
                "MCP-backbone",
                backbone_proc,
                WORK / "mcp-backbone.log",
            )
            # The gateway checks its upstream when it starts, so it goes second.
            with running(
                ["uv", "run", "--locked", "mcp-gateway", "serve", "--config", str(config)],
                gateway_env,
                gateway,
                WORK / "gateway.log",
            ) as gateway_proc:
                wait_healthy(
                    "http://127.0.0.1:8080/healthz",
                    "the gateway",
                    gateway_proc,
                    WORK / "gateway.log",
                )
                await agent(caller_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the end-to-end path locally.")
    parser.add_argument("--backbone", type=Path, required=True, help="MCP-backbone checkout")
    parser.add_argument("--gateway", type=Path, required=True, help="mcp-gateway repo")
    parser.add_argument("--gateway-ref", default="origin/main")
    args = parser.parse_args()
    asyncio.run(run(args.backbone.resolve(), args.gateway.resolve(), args.gateway_ref))


if __name__ == "__main__":
    main()
