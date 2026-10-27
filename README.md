# agent-observability

One tracing pipeline across a set of agent projects. Each project emits
OpenTelemetry traces that follow the GenAI semantic conventions, into one
small, self-hosted trace viewer (Jaeger v2), so a single agent request can
be followed from the agent through an MCP server to the database, with the
latency and the cost of every step.

**Status:** two sources are in, at $0. The end-to-end trace through the
MCP security gateway and the eval harness is the next slice
([Phase 7](docs/planning/phase-7-slices-plan.md)).

| Source | How | State |
|---|---|---|
| [MCP-backbone](https://github.com/NabilRobert/MCP-backbone) (MCP server) | Live: a span per stage of every tool call, under the MCP SDK's own span | Built, on a branch there |
| [Pitstop](https://github.com/NabilRobert/pitstop-crew-public) (agent pipeline) | Replayed: its 62 recorded runs (653 steps) from a backup, with the original times | Built, checked |
| MCP security gateway | Live, built in its own project | Next |
| agent-eval-harness | Live, a scripted agent ($0) | Next |

## What it looks like

An agent process calling MCP-backbone: one trace across two processes.
The agent's trace context travels in each MCP request's `_meta`; the
refused `DELETE` is marked on the stage that refused it (`validate`), and
no SQL text is recorded.

![An MCP-backbone tool call in Jaeger](docs/img/mcp-backbone-trace.png)

A Pitstop daily run, replayed from its records: the workflow, its agents,
the critic's reviews and revisions, and every model call with its tokens
and cost.

![A replayed Pitstop run in Jaeger](docs/img/pitstop-replay-trace.png)

## What's checked

- **No content by default.** MCP-backbone records no SQL text, rejection
  reasons or error messages unless `APP_OTEL_CAPTURE_CONTENT` is on; a
  test plants a marker in every path a query's text can take (run,
  refused, errored, a failed audit write) and finds it in no span. The
  replay never reads Pitstop's prompts and responses.
- **Tracing can't hurt the traced system.** With the viewer down, tool
  calls still succeed, and a shutdown gives up flushing after 2 s,
  however many spans are queued (the SDK alone took 5.3 s with a full
  queue). Without an endpoint configured, nothing is exported at all.
- **The replay is faithful.** Every recorded step appears once, with the
  same parent, start time (to the microsecond) and duration; the
  model-call costs in each run add up to its recorded total (26 runs had
  one). `0 problems` on all 62 runs, checked against the viewer's API.
- **Cheap.** Tracing one MCP-backbone tool call costs about **85 µs** of
  CPU (112 µs with tracing on, 27 µs with it off), steady across nine
  processes: **0.3%** of a 28 ms call. Over 1,500 whole calls per mode,
  tracing with the viewer up showed no detectable difference (−0.27 ms,
  95% CI −0.58 to +0.05). With the viewer down it measured +0.52 ms
  (+0.15 to +0.91), within the drift between rounds as the machine's load
  rose. Details: [`docs/mcp-backbone-overhead.md`](docs/mcp-backbone-overhead.md).
- **Small.** Jaeger runs in 256 MB; it used 95 MiB with everything above
  loaded.
- Every new test was mutation-checked: 10 mutations in MCP-backbone and 10
  in the replay, all caught. One of them exposed a real bug, now fixed.

## Run it

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose -f viewer/compose.yaml up -d
```

```bash
uv run python viewer/smoke.py
```

The UI is at http://127.0.0.1:16686. On the shared server, follow
[`viewer/RUNBOOK.md`](viewer/RUNBOOK.md).

Replay Pitstop's traces (needs the private backup):

```bash
replay/pitstop/run.sh path/to/pitstop.dump
```

Trace MCP-backbone: set `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`
in its environment. The cross-process demo above is
[`demos/mcp_backbone_trace.py`](demos/mcp_backbone_trace.py).

Tests:

```bash
uv run pytest
```

## Docs

- [`docs/conventions.md`](docs/conventions.md): the span names and
  attributes every source follows.
- [`docs/planning/`](docs/planning/): Phases 1–7, from the problem brief
  to the slice plan, with the spike results.
