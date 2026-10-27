# Phase 1: Problem Brief

**Project:** one tracing pipeline across Arthur's agent projects. Each
project emits OpenTelemetry traces that follow the GenAI semantic
conventions, into one small, self-hosted trace viewer. One agent request
can then be followed agent → gateway → MCP server → database, with the
latency and the cost of every step.

It builds on the earlier portfolio pieces:
- [MCP-backbone](https://github.com/NabilRobert/MCP-backbone), a read-only
  database MCP server;
- [agent-eval-harness](https://github.com/NabilRobert/agent-eval-harness),
  which measures whether an agent works;
- [Pitstop](https://github.com/NabilRobert/pitstop-crew-public), an agent
  pipeline that ran unattended for about 11 weeks;
- an MCP security gateway, in progress, which sits between an agent and an
  MCP server.

## Problem

Each project can explain *its own* part of a request, in its own format:
- MCP-backbone writes an audit row per query: status, cost estimate,
  rejection reason. It has no timings per stage, and no link to the agent
  that asked.
- The harness records Inspect AI logs per sample: the model's messages and
  tool calls. It can't see inside a tool call.
- Pitstop has its own Run Inspector: a tree of steps per run in its own
  tables, readable only in its own UI.
- The gateway will add a fourth format.

So a question such as *"this agent answer was slow and cost 4 cents: where
did the time and money go?"* needs four tools and a guess at how their
records line up. Nothing joins them, because nothing carries a shared
request ID across the process boundaries.

The fix is standard, not novel: OpenTelemetry traces, W3C trace context
across every hop, and one viewer. The value is in doing it properly across
real projects: standard attribute names, nothing sensitive recorded by
default, and tracing that can never break the system it traces.

## Affected parties

| Party | Wants |
|---|---|
| Arthur (builder) | To debug a slow or expensive agent request across projects in one place; evidence that he can run agent systems observably |
| Reviewer / AI engineering team | Proof of production habits: standard conventions, privacy by default, measured overhead, a small footprint |
| Someone running MCP servers | A worked example of tracing across MCP's process boundary, which is new in the MCP spec and SDK |

## How it's handled today, and where it breaks

- **Audit rows and logs per project.** Each is correct on its own, but
  they can't be joined, and none of them records time per stage.
- **Pitstop's Run Inspector** is the closest thing to tracing, but it is
  Pitstop-only, its schema is its own, and the pipeline no longer runs.
  Its recorded traces (62 runs from 2026-09-28 on) are in a private
  backup.
- **The MCP Python SDK** (2.x) already opens a span per MCP request and
  carries trace context in the request's `_meta`. Nothing in Arthur's
  projects installs a tracer provider, so those spans go nowhere.

## Success metrics (verifiable)

1. **One request, one trace.** An agent request run through the harness,
   the gateway and MCP-backbone to Postgres appears as a single trace (one
   trace ID) with spans from at least three services, correctly nested.
   *(Final slice: it needs the gateway and the harness, which join later.)*
2. **Latency and cost at every step.** Every model-call span carries
   tokens and cost; every database-touching span carries its duration and
   outcome. For each replayed Pitstop run, the sum of model-call costs
   matches the run's recorded cost (to rounding), checked by a test.
3. **Faithful replay.** For every replayed Pitstop run, the viewer holds
   exactly one span per recorded step, with the same parent, start time and
   duration, checked by a script against the viewer's API.
4. **Nothing sensitive by default.** No SQL text, prompts, responses,
   free-text summaries or secrets in any span unless an opt-in setting is
   on. A test asserts it for MCP-backbone; the Pitstop replay never reads
   the prompt and response table at all.
5. **Tracing can't break the traced system.** With the viewer down,
   MCP-backbone's tool calls still succeed, with no added error and no
   stall (a test points the exporter at a closed port).
6. **Measured overhead.** The latency tracing adds to an MCP-backbone tool
   call is measured and published, with tracing on and off. With tracing
   off, the server makes no export connections at all.
7. **Small footprint.** The viewer runs under a 256 MB memory limit on a
   server shared with other projects; its disk use is bounded by a
   retention period. Idle memory is measured and published.
8. **A reviewer understands the pipeline in about 15 minutes** from the
   README, the conventions doc and the screenshots.

## Non-goals (current pass)

- **No new model calls.** The budget is $0, so Pitstop's traces are
  replayed from its records, and model calls in later slices use recorded
  or scripted responses.
- **Traces only.** No metrics or log pipelines, dashboards or alerting in
  this pass.
- **No hosted SaaS** (Langfuse Cloud, Honeycomb, Datadog). Self-hosted,
  so the data stays on Arthur's machines.
- **No public viewer.** The trace viewer is reachable only by Arthur; the
  public proof is screenshots and the code.
- **No changes to Pitstop's code.** Its pipeline has stopped; its records
  are the evidence.
- **No sampling strategy.** The volume is tiny, so every trace is kept.

## Decisions (settled 2026-10-27)

1. **Repo:** `agent-observability` (planned public name
   `NabilRobert/agent-observability`). Arthur creates the remote and
   pushes.
2. **Budget:** $0 for model calls.
3. **Order of sources:** MCP-backbone and the Pitstop replay first; the
   gateway (instrumented in its own project) and the harness (once its
   in-flight branch is merged) after.
4. **Pitstop's evidence source:** the 2026-10-26 backup, restored into a
   throwaway local container. The live database is never touched, and the
   backup is never committed.

## Open questions (for Phase 2)

- **How the viewer is reached on the server:** an SSH tunnel only, or a
  login in front of it? (Recommended: tunnel only. See Phase 5.)
- **Whether replayed span data may be published** as a fixture, given that
  it holds only names, timings, models, tokens and costs.

## Cost of doing nothing

Each project stays explainable only on its own. The portfolio claims
"reliable agent systems" but can't show one request crossing four
components, and the MCP SDK's own trace spans keep going nowhere.
