"""The end-to-end check (Phase 1, metric 1): one agent request is one trace
that crosses every service on the path, correctly nested, down to the
database.

    uv run python -m e2e.check [TRACE_ID] [--service NAME ...]

With no trace ID, it checks the most recent trace that the first service
started. Exits non-zero, saying what's wrong, if the trace is broken:
a span whose parent isn't in the trace (context lost at a hop), more than
one root, a service missing, or no path from the agent down to a
database stage through every service in order."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from replay.common import StoredSpan, fetch_trace, query_traces

# The path, outermost first: the agent, the gateway, the MCP server.
DEFAULT_SERVICES = ("agent-eval-harness", "mcp-security-gateway", "mcp-backbone")
# MCP-backbone's stages that touch Postgres (docs/conventions.md).
DATABASE_STAGES = frozenset({"cost_check", "execute", "audit"})


def check_trace(stored: Sequence[StoredSpan], services: Sequence[str]) -> list[str]:
    """What's wrong with the trace, or [] if it holds the whole path."""
    if not stored:
        return ["the trace is empty or wasn't found"]
    problems: list[str] = []
    by_id = {s.span_id: s for s in stored}
    roots = [s for s in stored if s.parent_id is None]
    if len(roots) != 1:
        problems.append(f"{len(roots)} root spans, not 1: the trace is split")
    broken = [s for s in stored if s.parent_id is not None and s.parent_id not in by_id]
    for s in broken:
        problems.append(
            f"{s.service or '?'} span {s.name!r} has a parent that isn't in the trace "
            "(trace context lost at a hop)"
        )
    present = {s.service for s in stored}
    missing = [svc for svc in services if svc not in present]
    if missing:
        problems.append(f"services missing from the trace: {', '.join(missing)}")

    def path(span: StoredSpan) -> list[str]:
        """The services from the root down to this span, each once."""
        chain: list[str] = []
        seen: set[int] = set()
        node: StoredSpan | None = span
        while node is not None and node.span_id not in seen:
            seen.add(node.span_id)
            if not chain or chain[-1] != node.service:
                chain.append(node.service)
            node = by_id.get(node.parent_id) if node.parent_id is not None else None
        return chain[::-1]

    database_spans = [s for s in stored if s.service == services[-1] and s.name in DATABASE_STAGES]
    if not database_spans:
        problems.append(
            f"no database stage ({', '.join(sorted(DATABASE_STAGES))}) from {services[-1]}"
        )
    elif not any(_in_order(path(s), services) for s in database_spans):
        seen_paths = sorted({" -> ".join(path(s)) for s in database_spans})
        problems.append(
            f"no database stage is reached through {' -> '.join(services)}; "
            f"paths found: {'; '.join(seen_paths)}"
        )
    return problems


def _in_order(chain: Sequence[str], services: Sequence[str]) -> bool:
    """Whether the services appear in this order along the chain, each
    directly under the one before (nothing else in between)."""
    try:
        start = chain.index(services[0])
    except ValueError:
        return False
    return list(chain[start : start + len(services)]) == list(services)


def latest_trace(service: str, since: timedelta) -> int | None:
    """The most recent trace started by `service` within `since`."""
    now = datetime.now(UTC)
    spans = query_traces(service, now - since, now)
    roots = [(s.start, s.trace_id) for s in spans if s.parent_id is None and s.service == service]
    return max(roots)[1] if roots else None


app = typer.Typer(add_completion=False)


@app.command()
def main(
    trace_id: Annotated[
        str | None, typer.Argument(help="hex trace ID; default: the latest")
    ] = None,
    service: Annotated[
        list[str] | None,
        typer.Option("--service", "-s", help="the services on the path, outermost first"),
    ] = None,
    since_minutes: Annotated[int, typer.Option(help="how far back to look for the latest")] = 60,
) -> None:
    services = tuple(service or DEFAULT_SERVICES)
    tid = (
        int(trace_id, 16)
        if trace_id
        else latest_trace(services[0], timedelta(minutes=since_minutes))
    )
    if tid is None:
        typer.echo(
            f"FAIL: no trace started by {services[0]} in the last {since_minutes} min", err=True
        )
        raise typer.Exit(1)
    stored = fetch_trace(tid)
    problems = check_trace(stored, services)
    for p in problems:
        typer.echo(p, err=True)
    found = sorted({s.service for s in stored})
    typer.echo(
        f"{'FAIL' if problems else 'ok'}: trace {tid:032x}, {len(stored)} spans from "
        f"{len(found)} services ({', '.join(found)}); path {' -> '.join(services)}"
    )
    if problems:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
