"""Measures what tracing adds to an MCP-backbone tool call (Phase 1,
metric 6; Phase 7, slice 6), in three modes:

- off:  no endpoint set, so no tracer provider: every span is a no-op;
- on:   exporting to the local viewer;
- dead: exporting to a closed port, as when the viewer is down.

Each mode runs in a fresh process (the global tracer provider can be set
only once), on the same seeded test database, and calls `run_query`
through the real MCP protocol over in-memory streams, so the timings are
the server's own work plus the protocol, without network noise. Modes are
interleaved over several rounds, in a rotating order, so drift on the
machine spreads across all three. The measured process holds both ends of
the connection, so with tracing on it also records the MCP client's spans:
the overhead measured is an upper bound for the server's share.

A whole call takes tens of milliseconds, most of it database work, and
varies by more than tracing could add, so each process also times the
tracing work alone: building one call's span tree (the request span and
its six stages) through MCP-backbone's own tracer, many times over.

    uv run --project <MCP-backbone checkout> python demos/mcp_backbone_overhead.py <checkout>

Writes docs/mcp-backbone-overhead.md and .json. Needs the viewer running.
"""

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from backbone import seeded, use_test_database

LIVE = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
DEAD = "http://127.0.0.1:9"  # the discard port: nothing listens there
MODES = ["off", "on", "dead"]
SQL = "select title, rental_rate from film order by rental_rate desc limit 3"
TREES = 2000  # span trees timed per process
# A run is discarded if the 1-minute load average goes above the number of
# CPUs: other work on the machine then swamps what is being measured. (It
# happened: other sessions' builds pushed the load to 72 on 8 cores and the
# round medians to 29-103 ms.) A run won't start above half of that.
CPUS = os.cpu_count() or 1
OUT = Path(__file__).resolve().parent.parent / "docs" / "mcp-backbone-overhead"


async def measure(mode: str, calls: int, warmup: int) -> dict[str, object]:
    """One mode, in this process: time `calls` run_query calls after
    `warmup` untimed ones, then time the tracer's shutdown. With tracing
    on, it also records when the exporter's background thread was busy
    sending, and marks each call that overlapped a send, to see whether
    the slow calls are the ones that ran while an export was going on."""
    from app.mcp.server import mcp
    from app.tracing import configure_tracing, shutdown_tracing
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    exports = _record_exports()
    provider = configure_tracing()
    assert (provider is None) == (mode == "off"), f"tracing set up wrongly for mode {mode}"
    api_key = os.environ["OVERHEAD_API_KEY"]

    samples: list[float] = []
    windows: list[tuple[int, int]] = []
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        lowlevel = mcp._lowlevel_server  # pyright: ignore[reportPrivateUsage]
        server_task = asyncio.ensure_future(
            lowlevel.run(
                *server_streams, lowlevel.create_initialization_options(), raise_exceptions=False
            )
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                for i in range(warmup + calls):
                    started = time.perf_counter_ns()
                    result = await session.call_tool(
                        "run_query", {"sql": SQL}, meta={"api_key": api_key}
                    )
                    elapsed_us = (time.perf_counter_ns() - started) / 1000
                    status = (result.structured_content or {}).get("status")
                    if status != "succeeded":
                        sys.exit(f"call {i} in mode {mode}: {status}")
                    if i >= warmup:
                        samples.append(elapsed_us)
                        windows.append((started, started + int(elapsed_us * 1000)))
        finally:
            server_task.cancel()

    tree_us = _time_span_trees(TREES)

    started = time.perf_counter_ns()
    if provider is not None:
        shutdown_tracing(provider)
    shutdown_ms = (time.perf_counter_ns() - started) / 1e6
    return {
        "mode": mode,
        "samples_us": samples,
        "during_export": _overlaps(windows, exports),
        "exports": len(exports),
        "tree_us": tree_us,
        "shutdown_ms": shutdown_ms,
    }


def _record_exports() -> list[tuple[int, int]]:
    """Wraps the OTLP exporter's export() to log when each send ran (on
    the batch processor's thread). Only the measurement does this."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    sends: list[tuple[int, int]] = []
    original = OTLPSpanExporter.export

    def timed(self: OTLPSpanExporter, spans: Any) -> Any:
        started = time.perf_counter_ns()
        try:
            return original(self, spans)
        finally:
            sends.append((started, time.perf_counter_ns()))

    OTLPSpanExporter.export = timed  # type: ignore[method-assign]
    return sends


def _overlaps(windows: list[tuple[int, int]], sends: list[tuple[int, int]]) -> list[bool]:
    """For each call's (start, end), whether any export ran during it."""
    return [any(s < end and e > start for s, e in sends) for start, end in windows]


def _time_span_trees(n: int) -> list[float]:
    """The tracing work of one run_query call, without the call: the
    request span and its six stage spans, opened through MCP-backbone's
    own `tracer` and `stage()`, with the attributes the real ones carry."""
    from app.tracing import DB_ATTRIBUTES, stage, tracer
    from opentelemetry.trace import SpanKind

    timings: list[float] = []
    for _ in range(n):
        started = time.perf_counter_ns()
        with tracer.start_as_current_span(
            "tools/call run_query",
            kind=SpanKind.SERVER,
            attributes={"mcp.method.name": "tools/call", "gen_ai.tool.name": "run_query"},
        ) as request:
            for name in ("authenticate", "rate_limit", "validate"):
                with stage(name):
                    pass
            with stage("cost_check", kind=SpanKind.CLIENT, attributes=DB_ATTRIBUTES) as span:
                span.set_attribute("mcp_backbone.cost.estimate", 1.0)
            with stage("execute", kind=SpanKind.CLIENT, attributes=DB_ATTRIBUTES) as span:
                span.set_attribute("db.response.returned_rows", 3)
            request.set_attribute("mcp_backbone.query.status", "succeeded")
            with stage("audit", kind=SpanKind.CLIENT, attributes=DB_ATTRIBUTES):
                pass
        timings.append((time.perf_counter_ns() - started) / 1000)
    return timings


def _child_env(mode: str, api_key: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OVERHEAD_API_KEY"] = api_key
    if mode != "off":
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = LIVE if mode == "on" else DEAD
        env["OTEL_SERVICE_NAME"] = "mcp-backbone-overhead"
    return env


def _percentile(xs: list[float], p: float) -> float:
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def _diff_ci(a: list[float], b: list[float], p: float, rng: random.Random) -> tuple[float, float]:
    """95% bootstrap interval for percentile p of b minus that of a."""
    diffs = sorted(
        _percentile(rng.choices(b, k=len(b)), p) - _percentile(rng.choices(a, k=len(a)), p)
        for _ in range(2000)
    )
    return diffs[50], diffs[1949]


def _viewer_is_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:13133/status", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


async def run(checkout: Path, calls: int, warmup: int, rounds: int) -> None:
    if not _viewer_is_up():
        sys.exit("the viewer isn't running: docker compose -f viewer/compose.yaml up -d")
    if (load := os.getloadavg()[0]) > CPUS / 2:
        sys.exit(f"the machine is busy (load {load:.1f} on {CPUS} CPUs): rerun when it's idle")
    samples: dict[str, list[float]] = {m: [] for m in MODES}
    during: dict[str, list[bool]] = {m: [] for m in MODES}
    exports: dict[str, int] = {m: 0 for m in MODES}
    trees: dict[str, list[float]] = {m: [] for m in MODES}
    shutdowns: dict[str, list[float]] = {m: [] for m in MODES}
    per_round: list[dict[str, object]] = []
    loads: list[float] = []
    # Its own rate limit, high: 3 modes x rounds x (warmup + calls) queries.
    async with seeded(query_rate_limit=10**9) as api_key:
        for r in range(rounds):
            order = MODES[r % 3 :] + MODES[: r % 3]
            for mode in order:
                loads.append(os.getloadavg()[0])
                out = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, __file__, "measure", mode, str(calls), str(warmup)],
                    env=_child_env(mode, api_key),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                result = json.loads(out.stdout.strip().splitlines()[-1])
                samples[mode] += result["samples_us"]
                trees[mode] += result["tree_us"]
                during[mode] += result["during_export"]
                exports[mode] += result["exports"]
                shutdowns[mode].append(result["shutdown_ms"])
                loads.append(os.getloadavg()[0])
                per_round.append(
                    {
                        "round": r + 1,
                        "mode": mode,
                        "call_p50_us": statistics.median(result["samples_us"]),
                        "span_tree_p50_us": statistics.median(result["tree_us"]),
                    }
                )
                print(
                    f"round {r + 1}, {mode}: p50 {statistics.median(result['samples_us']):.0f} µs"
                    f" (load {loads[-1]:.1f})"
                )

    if max(loads) > CPUS:
        sys.exit(
            f"discarded: the load average reached {max(loads):.1f} on {CPUS} CPUs during the"
            " run, so other work swamped the measurement. Nothing written; rerun when idle."
        )

    rng = random.Random(6)
    stats: dict[str, dict[str, float]] = {}
    for mode in MODES:
        xs = samples[mode]
        stats[mode] = {
            "calls": len(xs),
            "p50_us": statistics.median(xs),
            "p95_us": _percentile(xs, 95),
            "p99_us": _percentile(xs, 99),
            "mean_us": statistics.fmean(xs),
            "shutdown_ms_max": max(shutdowns[mode]),
            "span_tree_p50_us": statistics.median(trees[mode]),
            "span_tree_p95_us": _percentile(trees[mode], 95),
        }
        if mode != "off":
            for p in (50, 95, 99):
                lo, hi = _diff_ci(samples["off"], xs, p, rng)
                added = stats[mode][f"p{p}_us"] - stats["off"][f"p{p}_us"]
                stats[mode][f"p{p}_added_us"] = added
                stats[mode][f"p{p}_added_ci95_us"] = [lo, hi]  # type: ignore[assignment]
            # The tail, call by call: are the calls slower than "off"'s p95
            # the ones that ran while an export was being sent?
            slow = stats["off"]["p95_us"]
            flagged = list(zip(xs, during[mode], strict=True))
            with_export = [x for x, d in flagged if d]
            without = [x for x, d in flagged if not d]
            stats[mode]["exports"] = exports[mode]
            stats[mode]["calls_during_export"] = len(with_export)
            stats[mode]["slow_share_during_export"] = (
                sum(x > slow for x in with_export) / len(with_export) if with_export else 0.0
            )
            stats[mode]["slow_share_otherwise"] = (
                sum(x > slow for x in without) / len(without) if without else 0.0
            )
    environment = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "machine": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "mcp": version("mcp"),
        "opentelemetry-sdk": version("opentelemetry-sdk"),
        "mcp_backbone_version": os.environ.get("APP_BUILD_VERSION") or _git_version(checkout),
        "database": "local Postgres in Docker (MCP-backbone's test database)",
        "calls_per_mode": calls * rounds,
        "warmup_per_process": warmup,
        "span_trees_per_mode": TREES * rounds,
        "rounds": rounds,
        "cpus": CPUS,
        "max_load_1min": max(loads),
    }
    OUT.with_suffix(".json").write_text(
        json.dumps({"environment": environment, "results": stats, "per_round": per_round}, indent=2)
        + "\n"
    )
    OUT.with_suffix(".md").write_text(_report(environment, stats, per_round))
    print(f"wrote {OUT.with_suffix('.md').name} and {OUT.with_suffix('.json').name}")


def _git_version(checkout: Path) -> str:
    out = subprocess.run(
        ["git", "describe", "--always", "--dirty", "--abbrev=12"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or "unknown"


def _report(
    env: dict[str, object], stats: dict[str, dict[str, float]], per_round: list[dict[str, object]]
) -> str:
    def ms(us: float) -> str:
        return f"{us / 1000:.2f} ms"

    labels = {"off": "Off (no endpoint)", "on": "On, viewer up", "dead": "On, viewer down"}

    def added(s: dict[str, float], p: int) -> str:
        lo, hi = s[f"p{p}_added_ci95_us"]  # type: ignore[misc]
        return f"{s[f'p{p}_added_us'] / 1000:+.2f} ms ({lo / 1000:+.2f} to {hi / 1000:+.2f})"

    calls, added_rows, tails, trees = [], [], [], []
    for mode, label in labels.items():
        s = stats[mode]
        calls.append(f"| {label} | {ms(s['p50_us'])} | {ms(s['p95_us'])} | {ms(s['p99_us'])} |")
        if mode != "off":
            added_rows.append(f"| {label} | {added(s, 50)} | {added(s, 95)} | {added(s, 99)} |")
            tails.append(
                f"| {label} | {s['exports']:.0f} | {s['calls_during_export']:.0f} of {s['calls']:.0f} "
                f"| {s['slow_share_during_export']:.1%} | {s['slow_share_otherwise']:.1%} |"
            )
        trees.append(
            f"| {label} | {s['span_tree_p50_us']:.1f} µs | {s['span_tree_p95_us']:.1f} µs "
            f"| {s['shutdown_ms_max']:.0f} ms |"
        )
    rounds = [
        f"| {p['round']} | {labels[str(p['mode'])]} | {ms(float(p['call_p50_us']))} "  # type: ignore[arg-type]
        f"| {float(p['span_tree_p50_us']):.1f} µs |"  # type: ignore[arg-type]
        for p in per_round
    ]
    off, on = stats["off"], stats["on"]
    tree_cost = on["span_tree_p50_us"] - off["span_tree_p50_us"]
    return f"""# MCP-backbone: what tracing costs a tool call

Generated by [`demos/mcp_backbone_overhead.py`](../demos/mcp_backbone_overhead.py)
on {env["date"]}. Do not edit by hand; rerun the script.

## The tracing work itself

Building one `run_query` call's span tree (the request span and its six
stage spans) through MCP-backbone's own tracer, timed
{env["span_trees_per_mode"]} times per mode:

| Tracing | p50 | p95 | Shutdown, worst |
|---|---|---|---|
{chr(10).join(trees)}

With the viewer up, tracing a call costs about **{tree_cost:.0f} µs** of
CPU at the median, against {off["span_tree_p50_us"]:.1f} µs when it is off
(no-op spans). That is {tree_cost / off["p50_us"]:.1%} of an untraced
call's median time below. The export runs in a background thread and
isn't part of this. "Shutdown" is the tracer's final flush when the
process stops (`shutdown_tracing`); with the viewer down it gives up at
its 2 s cap, and the spans still queued are lost.

## Whole tool calls

`run_query` with a small `SELECT`, through the real MCP protocol
(in-memory streams), {env["calls_per_mode"]} calls per mode, in a fresh
process per mode and round, {env["rounds"]} rounds in rotating order,
after {env["warmup_per_process"]} untimed calls per process:

| Tracing | p50 | p95 | p99 |
|---|---|---|---|
{chr(10).join(calls)}

Added compared with "off", with 95% bootstrap intervals (an interval
that includes zero is no detectable difference):

| Tracing | at p50 | at p95 | at p99 |
|---|---|---|---|
{chr(10).join(added_rows)}

A call takes about {ms(off["p50_us"])} at the median, most of it
database work (authentication, the rate-limit counter, the cost check,
the query and the audit write).

## The tail, call by call

With tracing on, the batch processor's thread sends spans every few
seconds, and in CPython it competes with the server's thread for the
interpreter lock while it serialises and sends them. Each timed call is
marked if an export ran at any point during it. "Slow" means slower than
the untraced p95 ({ms(off["p95_us"])}):

| Tracing | Exports | Calls during an export | Slow, during an export | Slow, otherwise |
|---|---|---|---|---|
{chr(10).join(tails)}

If exports caused the slow tail, the slow share during an export would be
well above the share otherwise. (With the viewer down, an "export" is a
failed send attempt.)

Per process, in the order run (a round's three modes ran back to back,
so drift shows up as whole rounds moving together):

| Round | Tracing | Call p50 | Span tree p50 |
|---|---|---|---|
{chr(10).join(rounds)}

## Limits

- One laptop, a local database, one cheap query.
- The process holds both ends of the MCP connection, so with tracing on
  it also records the MCP client's spans: the added time is an upper
  bound for the server's share.
- A real deployment adds network time, which tracing doesn't change.
- "Off" is the default: with no endpoint there is no tracer provider and
  no export connection.

Environment: {env["machine"]} ({env["cpus"]} CPUs; the 1-minute load
average stayed at or below {env["max_load_1min"]:.1f}), Python {env["python"]}, mcp {env["mcp"]},
opentelemetry-sdk {env["opentelemetry-sdk"]}, MCP-backbone
`{env["mcp_backbone_version"]}`, {env["database"]}.
"""


def main() -> None:
    if sys.argv[1:2] == ["measure"]:
        mode, calls, warmup = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        print(json.dumps(asyncio.run(measure(mode, calls, warmup))))
        return
    parser = argparse.ArgumentParser(description="Measure MCP-backbone's tracing overhead.")
    parser.add_argument("checkout", type=Path, help="the MCP-backbone checkout to measure")
    parser.add_argument("--calls", type=int, default=500, help="timed calls per mode per round")
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    use_test_database(args.checkout.resolve())
    asyncio.run(run(args.checkout.resolve(), args.calls, args.warmup, args.rounds))


if __name__ == "__main__":
    main()
