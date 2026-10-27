# Phase 4: Flows & Failures

## Happy path: the viewer

1. `docker compose up -d` in `viewer/` starts Jaeger with its config file
   mounted read-only, its Badger data on a named volume, and every port
   bound to `127.0.0.1`.
2. The smoke script sends one span over OTLP/HTTP and reads it back from
   the query API. Pass → the viewer is ready.

## Happy path: a live MCP-backbone call

1. **Start.** If `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the server installs a
   tracer provider (resource `service.name=mcp-backbone` and its build
   version) with a batch processor and the OTLP/HTTP exporter. If not,
   nothing is installed.
2. **The request arrives.** The MCP SDK's middleware extracts any trace
   context from `params._meta` and opens the `tools/call <tool>` server
   span: as a child of the caller's span if there was one, as a new root
   otherwise.
3. **Stages.** The tool opens one child span per stage (Phase 3). Each
   records its outcome. The stage that ends a request early sets
   `error.type` and the span status. No SQL text is recorded unless
   content capture is on.
4. **The response** is sent. The spans end and go into the batch queue.
5. **Export.** In the background, every few seconds or when a batch fills,
   the exporter posts the batch to the viewer.
6. **Read.** Arthur finds the trace in the UI by service, operation, tag
   or trace ID.

## Happy path: the Pitstop replay

1. **Restore.** Start a throwaway `postgres:16` container (pinned by
   digest, no published port) with the snapshot mounted read-only, and
   restore it.
2. **Export.** Run one read-only query: every `trace_events` row with its
   run's type and recorded cost, as JSON, into the git-ignored output
   folder. `trace_payloads` is never read.
3. **Remove the container.** The restore exists only for this step.
4. **Map.** Each recorded step becomes a span (the conventions doc's
   mapping): IDs from its run and step IDs, the parent from `parent_id`,
   the recorded start and end, the attributes and the status.
5. **Send.** Hand the spans to the OTLP exporter in batches; flush.
6. **Check.**
   - *Fidelity:* for each run, fetch the trace from the query API and
     compare it with the export file: span count, parents, start times and
     durations.
   - *Cost:* the model-call costs in each run add up to the run's recorded
     cost, to rounding.

## Source of truth

- **Each system's own records** are the truth: MCP-backbone's audit table
  and Pitstop's tables. Traces are a derived view, and may be incomplete
  (a dropped batch) without anything being wrong in the system.
- **The snapshot** is the truth for the replay. Its file name is recorded
  on every replayed span's resource; its SHA-256 is printed by the replay
  script.
- **`docs/conventions.md`** is the truth for names and attributes.

## Failure interrogation: a live call

| Step | Fail | Slow | Twice | Partial |
|---|---|---|---|---|
| Install the provider (1) | A bad endpoint value or a missing package → log one warning and run untraced. Never refuse to start for a tracing reason | N/A (no network at start) | Installing twice is refused by the SDK; done once, at startup | N/A |
| Extract context (2) | A malformed `traceparent` → the SDK ignores it and starts a new root (checked in its code) | N/A | N/A | A `tracestate` without a valid `traceparent` → ignored |
| Stage spans (3) | An exception inside a span is recorded on it and re-raised **unchanged**, so error handling and responses stay exactly as they are | Span calls are in-memory and take microseconds; measured in slice 6 | N/A | A crash mid-request loses those spans (never ended, never exported). The audit table still has the truth |
| Queue (4) | Queue full → new spans dropped, counted by the SDK, never blocking | N/A | N/A | N/A |
| Export (5) | Viewer down → the exporter's post fails in the background thread, is logged, and the batch is dropped. **The request already returned** | The exporter has a timeout (10 s default); it runs in the background, so only the export thread waits | If a retried post stored a batch twice, the viewer would show duplicate spans: cosmetic, and the system's own records are unaffected | A cut connection → that batch lost |
| Shutdown | Flush pending spans with a short timeout, so a dead viewer can't hold up a server restart | As left | N/A | Unflushed spans are lost |

## Failure interrogation: the replay

| Step | Fail | Slow | Twice | Partial |
|---|---|---|---|---|
| Restore (1) | Missing dump or a failed restore → stop with a non-zero exit, before anything is sent. The container is removed on every exit path | The snapshot is about 3 MB; a few seconds | Each run uses a fresh container name | `pg_restore --exit-on-error`, so there is no partial restore |
| Export (2) | Query error → stop; nothing sent | N/A | Same file each time (same snapshot) | N/A |
| Map (4) | A step whose parent isn't in its run, or a run with other than one root → stop and name it. (Checked on the snapshot during planning: none) | N/A | Same output (pure function) | **A missing end time** (a step that never finished) → the span ends at its start, with `pitstop.step.unfinished=true`. None in the snapshot, but handled |
| Send (5) | Viewer down → the exporter reports failure; the script exits non-zero and says so | 653 spans; seconds | **Same IDs, so the same trace.** Jaeger may then show duplicate spans in a trace; the fidelity check catches this, and the fix is a fresh volume | A failed batch → the fidelity check names the runs that are short |
| Check (6) | A mismatch → exit non-zero, listing each run and what differs | N/A | N/A | N/A |

## Known quirks in the records (found while planning)

- **The daily email outlives its run.** In all 7 traced daily runs, the
  `Send daily email` step ends 0.4–0.6 s after the run's own span. The
  replay keeps the recorded times; the viewer draws the email past the end
  of its parent. It is written up rather than "fixed".
- **Run cost on the root.** 26 of the 62 run spans carry the run's total
  cost, matching `pipeline_runs` exactly. Per ADR-004 it doesn't become
  `agentobs.cost.usd` on the root (that would double-count); it is kept as
  `pitstop.run.recorded_cost_usd`, which the cost check compares against.
- **Two model calls have no model name.** Both failed; their spans are
  named `chat`, with the error recorded.

## Failure interrogation: the end-to-end trace (later)

- **A hop that drops `_meta`** splits the trace in two: the server's spans
  become a new root. The end-to-end test asserts one trace ID across all
  sources, so this fails loudly.
- **Clock skew between machines** shifts a child outside its parent on
  screen. All sources run on one machine or one server today; if not, the
  write-up says so.
- **The gateway records content** that MCP-backbone keeps out (arguments,
  outputs). The conventions doc's content rule applies to every source;
  the gateway session is given it.
