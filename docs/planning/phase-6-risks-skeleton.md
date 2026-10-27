# Phase 6: Risks, Spikes and the Walking Skeleton

## Risks, ranked by likelihood × impact

**Technical**

- **Old timestamps fall outside retention.** Pitstop's spans are dated
  28 September to 25 October. If Badger's retention counts from a span's
  own start time rather than from when it was written, a replayed run
  could expire at once, or never show up in a search. **Likelihood:
  medium. Impact: high** (slice 3 shows nothing). → Spike 1.
- **Our spans don't nest under the SDK's.** The MCP SDK opens its span in
  middleware; the tool function may run in another task, where the span
  isn't the current context. The stage spans would then be separate roots.
  **Likelihood: medium. Impact: high** (the MCP-backbone tree is the main
  exhibit). → Spike 2.
- **A dead viewer stalls the server.** If an export or the shutdown flush
  blocks on a closed port, a restart or a request could wait for the
  exporter's timeout. **Likelihood: low–medium. Impact: high** (tracing
  would hurt the system it observes). → Spike 3.
- **The replay can't set its own IDs and times.** The SDK's normal
  tracer generates its own IDs and uses the current time. Building spans
  directly for the exporter may hit private APIs. **Likelihood: low.
  Impact: medium** (the replay would have to fall back to random IDs, and
  repeat runs would duplicate). → Spike 4.
- **Jaeger goes over 256 MB.** **Likelihood: low** at this volume.
  **Impact: medium** (the server is shared). → Measured in Spike 1.
- **The conventions move.** The GenAI and MCP conventions are at
  "Development" status and were recently moved to their own repository.
  **Likelihood: high. Impact: low**, with the revision pinned in the
  conventions doc.

**Integration and process**

- **The gateway session traces differently.** It builds its tracing in
  parallel and may pick other names, or drop `_meta`. **Likelihood:
  medium. Impact: medium** (the end-to-end trace breaks or reads
  inconsistently). → The conventions doc and a short note are handed to
  that session before it starts on tracing; the end-to-end test catches a
  split trace.
- **The harness branch stays unmerged.** **Likelihood: medium. Impact:
  medium** (metric 1 waits). Slices 1–3 stand on their own.
- **Private data leaks into the repo.** The export file or a screenshot
  could hold synthetic-but-private content. **Likelihood: low. Impact:
  high.** → Git-ignored output folder; content capture off; screenshots
  checked before they are committed.

## Pre-mortem

*"It's a month from now and the project failed. Why?"*
1. The traces looked right on the laptop but recorded SQL text on the
   server, because a default changed. → A test asserts that no attribute
   holds the query text, with content capture off.
2. Jaeger ate memory on the shared server and was killed along with its
   neighbours. → The memory limit is in the compose file, and the idle and
   loaded figures are measured before the server step.
3. The end-to-end trace never happened, because each project traced in
   isolation. → The conventions doc comes first, and the gateway session
   gets it now, not after it has finished.

## Spikes (time-boxed, throwaway, all $0)

| # | Question | How | Pass if | Time box |
|---|---|---|---|---|
| 1 | Does Jaeger keep and find spans dated weeks ago, and how much memory does it use? | Start Jaeger v2 with Badger and the planned retention; send spans dated 30 days ago and today; search for both; restart the container and search again; read its memory use | Both found, before and after a restart; idle memory well under 256 MB | 45 min |
| 2 | Do spans opened in a tool function nest under the SDK's `tools/call` span, and is `_meta` context honoured? | MCP-backbone's in-memory test client, an in-memory span exporter, one span in a tool; call it inside a client-side span | The tool's span is a child of `tools/call <tool>`, which is a child of the client's span, all in one trace | 45 min |
| 3 | Does a dead exporter delay a request or a shutdown? | The OTLP exporter pointed at a closed port; time 50 calls of a function that opens a span, then time `shutdown()` | No measurable delay per call; shutdown bounded by a timeout we set | 30 min |
| 4 | Can a span be built with our own IDs, parent and times, and exported? | Build spans with the SDK's `ReadableSpan` and fixed IDs, export to Jaeger, read them back from the query API | IDs, parent and times come back exactly as sent, using only public API | 30 min |

Order: 2 → 4 → 1 → 3. Spike 2 comes first because the MCP-backbone tree
is the main exhibit and there is no simple fallback for it.

Spike 1 needs the Jaeger image (about 50 MB). Spikes 2 and 4 need the
OpenTelemetry SDK and OTLP exporter packages (a few MB).

## Walking skeleton

The thinnest path from a source to a screen, before any part is deep:
- the viewer running locally;
- MCP-backbone with the provider installed and **one** stage span
  (`validate`), visible under the SDK's span in the viewer;
- the replayer sending **one** recorded run.

Then slices 2 and 3 deepen each: every stage, the tests, the full replay
and its checks.

## Spike results

Run 2026-10-27 on Arthur's laptop (Apple silicon, Docker Desktop), in
the order 2 → 4 → 1 → 3. All four passed. The scripts are in
[`spikes/`](../../spikes/).

| # | Result | Consequence for the build |
|---|---|---|
| 2 | **Pass.** With mcp 2.3.0 over in-memory streams: a span opened in a tool is a child of the SDK's `tools/call <tool>` server span, which is a child of the client's `MCP send tools/call <tool>` span (a remote parent, through `_meta`), which is a child of the caller's own span. One trace ID throughout. The client also issues a `tools/list` inside `call_tool`, which shows up in the same trace | ADR-003 and ADR-005 stand. MCP-backbone only installs a provider and opens its stage spans |
| 4 | **Pass.** Spans built as the SDK's public `ReadableSpan`, with fixed trace and span IDs, a parent and timestamps 0, 20 and 40 days old, came back from Jaeger's query API identical: IDs, parents, start and end to the nanosecond | ADR-006 stands: the replay builds spans directly |
| 1 | **Pass.** The 20- and 40-day-old spans were found by ID and by a time-range search, before and after a container restart: Badger's retention counts from when a span is written, not from its start time. Jaeger used **15 MiB** of memory idle, against the 256 MiB limit. Badger preallocates a 2.3 GB value log, but it is sparse: 44 KB actually used | Retention of 30 days is safe for the replay. Disk use is reported as real use, not apparent size. Jaeger 2.22 serves only its v3 query API (`/api/v3/...`); the old `/api/traces` is gone, so the checks use v3 |
| 3 | **Pass, with a setting.** A closed-port exporter added nothing measurable per call (p50 about 45 µs for five spans, with or without an exporter). But `shutdown()` waited **6.5 s** with the exporter's default timeout, and **1.1 s** with a 2 s timeout | MCP-backbone sets a 2 s exporter timeout by default, so a dead viewer can't hold up a restart |

**As built (slices 1–3):** after the full Pitstop replay (653 spans, sent
twice) plus the MCP-backbone tests and demo, Jaeger used **95 MiB**
against its 256 MiB limit, and **4.2 MB** of real disk. Replaying twice
stored no duplicates: Jaeger keys spans by ID, so the replay is
idempotent. A mutation check in slice 2 found a real bug: the exporter
read its endpoint from the process environment, not the one passed in,
so a test's "dead" viewer was in fact the live local one.

