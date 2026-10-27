# Phase 3: Domain Model

## Glossary

**Tracing**

- **Source**: one program that emits spans, identified by its
  `service.name`: `mcp-backbone`, `pitstop`, later the gateway and the
  harness. A source's **resource** holds the attributes that describe it
  as a whole (name, version, and for a replay, `agentobs.replay`).
- **Trace**: everything done for one request, identified by a 16-byte
  **trace ID**. It can span several sources.
- **Span**: one timed unit of work in a trace: a name, a kind, a start and
  an end, a status, attributes, and a parent (except the root). A span's
  **kind** says what role it plays: `SERVER` (handling an incoming call),
  `CLIENT` (making an outgoing call, such as a model or database call) or
  `INTERNAL`.
- **Remote parent**: a parent span in another process. The child learns
  about it only from the **trace context** carried with the request.
- **Trace context**: the W3C `traceparent` (trace ID, parent span ID,
  flags) and `tracestate`. Its **carrier** is where it travels: an MCP
  request's `params._meta`, or an HTTP header (ADR-003).
- **Attribute**: a key and a value on a span or resource. **Standard**
  attributes come from the OpenTelemetry conventions; **namespaced** ones
  are this project's own (`agentobs.*`) or a source's own
  (`mcp_backbone.*`, `pitstop.*`).
- **Content**: any attribute that can hold user or model text: SQL text,
  prompts, responses, summaries, free-text reasons. **Content capture** is
  opt-in, per source, and off by default.
- **Cost**: `agentobs.cost.usd`, the money one span spent itself. Only
  model-call spans have it today.
- **The conventions**: `docs/conventions.md`, the one mapping every source
  follows, pinned to a revision of the upstream conventions.

**The pipeline**

- **Tracer provider**: the SDK object a source installs at startup. With
  none installed, spans are created but not recorded (a no-op).
- **Batch processor**: holds finished spans in a bounded queue and hands
  them to the exporter in the background. When the queue is full, new
  spans are dropped, never waited for.
- **Exporter**: sends batches over OTLP/HTTP to the viewer.
- **Viewer**: Jaeger (ADR-001). Its **receiver** takes OTLP; its
  **storage** (Badger) keeps traces for the **retention** period; its
  **UI** and **query API** read them back.

**The replay**

- **Snapshot**: the 2026-10-26 Pitstop backup. Private; never committed.
- **Throwaway restore**: a local container holding the snapshot, deleted
  after use.
- **Recorded step**: one row of Pitstop's `trace_events`: kind, name,
  status, start and end, model, tokens, cost, a short summary and a JSON
  `meta`. Steps form a tree per pipeline run.
- **Export file**: the recorded steps as JSON, written to a git-ignored
  folder. It holds no prompts or responses, since `trace_payloads` is never
  read.
- **Replayed span**: the span made from one recorded step, with the step's
  own times, and IDs derived from its run and step IDs.
- **Fidelity check**: compares what the viewer returns for a replayed run
  with the export file, step by step.

## Entities & relationships

- `Source` → one `Resource`; emits many `Span`s.
- `Trace` → many `Span`s, from one or more `Source`s; exactly one root.
- `Span` → zero or one parent `Span` (local, or remote through a trace
  context); many children; many `Attribute`s.
- `MCP request` → carries zero or one `Trace context` in `params._meta`.
- `Pipeline run` (Pitstop) → one replayed `Trace`; `Recorded step` → one
  replayed `Span`.
- `Viewer` → stores `Trace`s for the retention period.

## The span trees, by source

**MCP-backbone, one `run_query` call** (ADR-005):

```
tools/call run_query          SERVER  (the MCP SDK's own span)
├── authenticate              INTERNAL
├── rate_limit                INTERNAL
├── validate                  INTERNAL
├── cost_check                CLIENT   db: EXPLAIN
├── execute                   CLIENT   db: the query
└── audit                     CLIENT   db: INSERT into the audit table
```

A call that ends early (refused, rejected) has only the spans up to the
step that ended it, then `audit`.

**Pitstop, one daily run** (replayed, ADR-006):

```
invoke_workflow daily
├── context steps             INTERNAL
├── invoke_agent <agent>      INTERNAL
│   ├── chat <model>          CLIENT   tokens, cost
│   ├── invoke_agent <critic> INTERNAL (a critic review)
│   │   └── chat <model>
│   ├── decision steps        INTERNAL
│   └── revision steps        INTERNAL
│       └── chat <model>
└── Send daily email          INTERNAL
```

**Later, end to end** (slices 5–6 in Phase 7):

```
invoke_agent <harness agent>            harness
├── chat <model>                        harness
└── execute_tool run_query              harness
    └── tools/call run_query  CLIENT    harness (the MCP SDK)
        └── tools/call run_query SERVER gateway
            └── tools/call run_query CLIENT gateway
                └── tools/call run_query SERVER mcp-backbone
                    └── validate, cost_check, execute, audit
```

## Invariants

- **Tracing never changes a request's outcome.** No span, exporter or
  viewer failure can fail, stall or alter a traced call.
- **Off unless configured.** Without an endpoint, a source installs no
  provider and makes no export connection.
- **No content by default.** No SQL text, prompt, response, summary or
  free-text reason leaves a source unless its content capture is on.
- **Cost on the spender only.** `agentobs.cost.usd` is never rolled up
  into a parent, so summing a trace's costs never double-counts.
- **Replayed times are the recorded times.** Nothing is shifted, and
  nothing is invented: a value missing in the records is missing in the
  span.
- **Replay is repeatable.** The same snapshot always gives the same trace
  and span IDs.
- **The private data stays private.** The snapshot and the export file
  are git-ignored; tests use a synthetic fixture.
- **Traces are a view, not the record.** Each system's own records
  (MCP-backbone's audit table, Pitstop's tables) remain the source of
  truth. A dropped span is acceptable; a missing audit row is not.
- **The viewer is never public.** Every port is bound to loopback.
