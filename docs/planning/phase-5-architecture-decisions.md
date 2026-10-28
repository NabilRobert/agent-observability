# Phase 5: Architecture Decisions

Short ADRs, settled 2026-10-27. Each gives the decision, why, the
alternatives rejected, and its consequences. The spikes in Phase 6 test
the riskiest of them before the build.

### ADR-001: The viewer is Jaeger v2, in one container, with Badger storage

**Decision:**
- `jaegertracing/jaeger` v2 (the all-in-one binary), pinned by digest.
- It receives OTLP over HTTP (4318) and serves its UI (16686), both bound
  to loopback.
- Traces are stored on disk in Badger, its embedded key-value store, with
  a retention period. The container has a 256 MB memory limit.

**Why:**
- **Memory first.** The server is small and shared, and Jaeger is the
  lightest viewer that does the job: one Go process, no separate database.
- **OTLP-native.** Jaeger v2 is built on the OpenTelemetry Collector, so
  the sources send standard OTLP, and moving to another backend later is a
  config change in the sources, not a code change.
- **It shows exactly what this project is about:** one request's span
  tree, with timings, a critical path and every attribute.

**Rejected:**
- *Grafana Tempo with Grafana:* two services to run, configure and keep
  within memory, instead of one. Its TraceQL could sum cost across spans,
  which Jaeger can't. Revisit if aggregating cost becomes a
  requirement.
- *SigNoz:* needs ClickHouse and its coordination service; far too heavy
  for a shared small server.
- *Langfuse (self-hosted v3):* the best LLM-specific UI, but it needs
  Postgres, ClickHouse, Redis and object storage.
- *Arize Phoenix:* one container with an LLM-aware UI, but it is built
  around the OpenInference attribute conventions rather than the
  OpenTelemetry GenAI ones, which this project sets out to follow.
- *OpenObserve:* a single light binary and a reasonable runner-up, but a
  smaller ecosystem and a UI aimed at logs first.
- *Jaeger with in-memory storage:* lighter still, but traces vanish on
  every restart, and the memory used grows with the traces kept.

**Consequences:**
- Jaeger can't sum an attribute across spans, so "total cost of a trace"
  is read off the model-call spans, not computed by the viewer.
- Badger's retention must cope with replayed spans dated weeks ago
  (Phase 6, Spike 1).
- The UI has no login, which decides ADR-007.

**As built (2026-10-28): in memory, not Badger.** Replaying the
harness's 1,777 runs into Badger peaked at 532 MiB, and the container was
killed at its 256 MB limit. Jaeger opens Badger with Badger's defaults
(five 64 MB write buffers, a 256 MB read cache) and exposes no setting to
shrink them, so the limit and Badger can't coexist under a bulk import.
The idle 15 MiB measured in Spike 1 hid this. The same data in Jaeger's
memory store peaked at 72 MiB, so the viewer now keeps at most 5,000
traces in memory. What that costs: a restart empties the viewer. The
replays are idempotent and put their traces back in seconds, and live
traces were always a view, not the record (Phase 3), so the trade is
worth it. *Rejected instead:* raising the limit to fit Badger (about
768 MB), too much for a shared small server.

### ADR-002: Export straight to the viewer over OTLP/HTTP

**Decision:**
- Each source uses its language's OpenTelemetry SDK with a batch span
  processor and the OTLP/HTTP (protobuf) exporter, sending straight to the
  viewer's receiver.
- Configured only through the standard variables
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, ...). With no
  endpoint set, no tracer provider is installed, and the SDK's spans cost
  next to nothing and go nowhere.

**Why:** the fewest moving parts. Jaeger v2 already is a Collector, so a
second one in front would add a container and do nothing.

**Rejected:**
- *A separate OpenTelemetry Collector:* useful for fan-out, tail sampling
  or redaction at a central point; none is needed yet. It can be added
  later without touching the sources.
- *OTLP over gRPC:* pulls in `grpcio`, a large native dependency, for no
  gain at this volume.
- *Jaeger's own (Thrift) protocol:* deprecated in favour of OTLP.

**Consequences:**
- A process that installs no provider still pays for nothing: the SDK's
  spans are no-ops.
- A batch lost while the viewer is down is lost for good (Phase 4).
- A central collector can be slotted in later by changing one variable.

### ADR-003: Trace context crosses MCP in `params._meta`

**Decision:**
- Across MCP hops, the W3C `traceparent` and `tracestate` travel in the
  request's `params._meta`, as the OpenTelemetry MCP conventions and MCP's
  SEP-414 specify. The MCP Python SDK 2.x already does this on both sides:
  a client injects the current context into every request, and a server
  opens its span as a child of what it extracts.
- A proxy, such as the gateway, is both: it extracts on the way in, and
  injects on the way out from inside its own span, so its span sits
  between the agent's and the server's.
- Non-MCP HTTP hops use the standard `traceparent` header.

**Why:** an HTTP header only covers one HTTP request, while MCP can run
over stdio or reuse one connection for many calls. `_meta` travels with
each call on every transport.

**Rejected:**
- *Only the HTTP header:* breaks on stdio and in the in-memory transport
  that the tests and the benchmark use.
- *A request-ID attribute joined afterwards:* it gives no parent-child
  structure, and every viewer would need custom joins.

**Consequences:**
- MCP-backbone and any SDK-based client get propagation for free; our
  work is to install a provider, not to carry context by hand.
- The gateway must forward `_meta` trace context; it is in the
  conventions doc and the note to its session.

### ADR-004: One attribute mapping, written down once

**Decision:** `docs/conventions.md` is the single reference for span names
and attributes, for every source. In short:
- The standard conventions (GenAI, MCP, database) wherever they cover the
  case, at the revision recorded in the doc.
- **Cost** isn't in the conventions. It is `agentobs.cost.usd` (a double,
  in US dollars) on the span that incurred it, never summed into its
  parents, so that adding up a trace never counts anything twice.
- Project-specific details use the project's own namespace, for example
  `mcp_backbone.query.status` or `pitstop.step.kind`.
- Content (SQL text, prompts, responses, summaries) is opt-in, under a
  per-project setting, and off by default.

**Why:** consistent names make a cross-project trace readable, and let a
viewer or a later query treat every source the same way.

**Rejected:**
- *`gen_ai.usage.cost` or a similar made-up `gen_ai.*` name:* it would
  squat on the standard's namespace, and could clash with a future
  standard attribute that means something else.
- *Cost rolled up into parent spans:* convenient to read, but it
  double-counts as soon as anything sums the trace.

**Consequences:**
- Cost questions are answered from the model-call spans alone.
- When the conventions change, the doc is updated and its pinned revision
  moved, in one commit, with the sources following.

### ADR-005: MCP-backbone gets hand-written spans, not auto-instrumentation

**Decision:**
- Install a tracer provider at startup when an endpoint is configured.
  The SDK's own `tools/call <tool>` server span becomes the parent.
- Add one child span per stage of `run_query`: `authenticate`,
  `rate_limit`, `validate`, `cost_check`, `execute` and `audit`. The other
  tools get `authenticate` and their one database step.
- Each span carries its outcome and, where it ends a request, a stable
  `error.type` (for example `rejected_by_cost`). The rejection's
  free-text reason is opt-in, since it can quote the query.

**Why:** the database auto-instrumentation records each statement's text.
The cost check runs `EXPLAIN <the user's SQL>`, so the user's query would
land in the trace by default. Six explicit spans are easy to read and to
test.

**Rejected:**
- *SQLAlchemy or asyncpg auto-instrumentation:* the default leak above,
  plus a span per internal statement, which buries the stages.
- *Auto-instrumentation with the statement text filtered out:* it works,
  but the stage spans would still be needed, so it adds noise and a
  dependency for nothing.
- *Replaying the audit table (`query_executions`) instead, as first
  sketched in the project handover:* one row per call, with no time per
  stage and no link to the agent that asked, so it can't join an
  end-to-end trace. The audit table stays the record (Phase 3); the spans
  are the timed view of the same calls.

**Consequences:**
- MCP-backbone takes two new dependencies (the SDK and the OTLP/HTTP
  exporter); the API package is already there, through the MCP SDK.
- The stage spans must nest under the SDK's own span (Phase 6, Spike 2).

### ADR-006: Pitstop is replayed from its records, not re-run

**Decision:**
- A script restores the backup into a throwaway local container (the
  same pinned `postgres:16` image as Project 3's evidence), exports
  `trace_events` (never `trace_payloads`, which holds the prompts and
  responses) as JSON, and removes the container.
- A replayer turns each row into a span with its **original** start and
  end times and its original parent, and sends them over the same OTLP
  exporter.
- Trace and span IDs are derived from the run and event IDs, so a replay
  run twice gives the same IDs.
- Every replayed span's resource carries `agentobs.replay = true`.

**Why:** the pipeline no longer runs and the budget is $0, but its 62
traced runs are real records of real model calls. A replay shows them in
the standard format without inventing anything.

**Rejected:**
- *Re-running the pipeline:* costs money, and the data source behind it
  is gone.
- *Shifting the timestamps to "now":* easier to find in the viewer, but
  it misstates when things happened.
- *Importing straight into Jaeger's storage:* ties the replay to one
  viewer; OTLP works with any.
- *Instrumenting Pitstop's Node code:* worth doing only if Pitstop runs
  again.

**Consequences:**
- The replay builds spans directly, with its own IDs and times
  (Phase 6, Spike 4).
- Finding the runs needs a search over September and October, not "the
  last hour".

### ADR-007: On the server, the viewer is reachable only by SSH tunnel

**Decision:**
- The same compose file runs on the shared server, with every port bound
  to loopback and the 256 MB memory limit.
- Services on the server send to the loopback OTLP port. Arthur opens the
  UI through an SSH tunnel.
- The deploy is a server step done by Arthur, from a short runbook.

**Why:** Jaeger's UI has no login, and every attribute is visible in it.
A tunnel adds no new password or public surface. The public proof is
screenshots.

**Rejected:**
- *The UI behind the server's reverse proxy with a password:* another
  public endpoint and secret to manage, for a viewer with one user.

**Consequences:**
- No public trace viewer to link to; the README carries screenshots.
- Sources on the server need no network change: they send to loopback.

### ADR-008: This repo is Python, with the same tooling as MCP-backbone

**Decision:** Python 3.12+, managed with `uv`; ruff, pyright (strict) and
pytest, as in MCP-backbone. The replayer depends only on the OpenTelemetry
SDK and the OTLP/HTTP exporter. The restore runs in Docker, as Project 3's
evidence script does, and the export is plain SQL, so no database driver
is needed.

**Why:** one toolchain across the portfolio, and the smallest dependency
set that does the job.

**Rejected:**
- *Node (Pitstop's language):* the replay reads Pitstop's data, not its
  code; Python matches the other sources.
- *A database driver (psycopg) to read the restore:* one more dependency,
  when `psql` in the same container can write the JSON.

**Consequences:**
- The replay runs anywhere Docker and `uv` do, with no credentials.

### ADR-009: The live end-to-end run reuses the harness's own scripted agent

Added 2026-10-28, under the rule "reuse before building".

**Decision:** slice 5(b), the one live trace from agent to database,
reuses what already exists and builds only the glue:
- **The agent:** the harness's scripted agent, `scripts/gullible_agent.py`
  (Inspect's `mockllm`, so $0), which already runs through the gateway
  with a gateway target.
- **The propagation:** the MCP Python SDK 2.3.0, already in the harness,
  injects the trace context into each request's `_meta` once a tracer
  provider is installed; the gateway and MCP-backbone pick it up on the
  same SDK.
- **The new part:** installing a tracer provider in the harness's process
  (off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, as in MCP-backbone),
  and one root span per sample, so the agent's side has a parent.

**Options considered:**
- *Inspect AI's own tracing:* none to reuse; version 0.3.283 has no
  OpenTelemetry code (checked in the harness's installed package).
- *A new demo agent, like `demos/mcp_backbone_trace.py`:* it would prove
  the path, but not through the harness, which is the point of the slice.
- *A third-party tracing layer (OpenInference, Langfuse's SDK):* whether
  either supports Inspect directly wasn't checked; either way they use
  their own attribute conventions, not the OpenTelemetry GenAI ones this
  project follows (ADR-004), and would add a dependency for what is a few
  lines of glue.

**Consequences:** the harness gains two dependencies (the OpenTelemetry
SDK and OTLP/HTTP exporter, as MCP-backbone has), and the change lives in
the harness's own repo, on a branch, per the rule.

## Sources

- OpenTelemetry GenAI conventions (spans, agent spans, MCP):
  https://github.com/open-telemetry/semantic-conventions-genai (the revision
  followed is pinned in `docs/conventions.md`).
- OpenTelemetry database conventions:
  https://opentelemetry.io/docs/specs/semconv/database/
- MCP `_meta` trace context: SEP-414, as implemented in the MCP Python SDK
  2.x (`mcp/shared/_otel.py`, `mcp/server/_otel.py`).
- Jaeger v2: https://www.jaegertracing.io/docs/
