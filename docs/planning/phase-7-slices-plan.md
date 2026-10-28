# Phase 7: Slice and Plan the Work

## Before the slices: the spikes

The four Phase 6 spikes, in the order 2 → 4 → 1 → 3, take about 2.5 hours
and cost $0. The results go into Phase 6 before slice 1 starts. A spike
that fails changes the plan below, and the change is recorded.

## Slices (vertical: each one runs end to end and is demoable)

| Slice | What it proves, end to end | Where | Model spend | Est. |
|---|---|---|---|---|
| **0. Planning** | Phases 1–7 and `docs/conventions.md` | This repo, `docs/planning` | None | 0.5 day |
| **1. Viewer** | `viewer/compose.yaml` and Jaeger's config, pinned by digest, loopback only, 256 MB, Badger with retention. A smoke script sends one span and reads it back. Idle memory measured | This repo | None | 1 h |
| **2. MCP-backbone** | The provider at startup (off unless configured); the six stage spans; tests: the tree's shape, no SQL text by default, the text present with capture on, `_meta` context honoured, a dead exporter harmless. Each test mutation-checked. Ruff, pyright and the full pytest suite pass. A `run_query` trace in the viewer (screenshot) | MCP-backbone, a new branch off `main` in its own worktree | None | 0.5 day |
| **3. Pitstop replay** | `replay/pitstop/`: restore-and-export script, the replayer, mapping tests on a synthetic fixture, the fidelity and cost checks against the viewer's API. All 62 traced runs in the viewer (screenshot of a daily run) | This repo | None | 0.5 day |
| **— Checkpoint —** | Slices 0–3: two sources in one viewer, at $0. Report to Arthur with screenshots and the decisions he needs to make | | | |
| **4. Gateway** | The gateway's own spans, passing `_meta` context through, following the conventions | The gateway's repo, by the gateway's session | None | (theirs) |
| **5. Harness** | After its in-flight branch merges: (a) its recorded Inspect run logs replayed as spans, the same way as Pitstop's, so real model calls with real tokens and cost appear at $0; (b) the harness's scripted agent (`scripts/gullible_agent.py`, `mockllm`) run live through the gateway to MCP-backbone, with a tracer provider and a root span per sample as the only new code (ADR-009), and an automated check that the whole request is one trace across three or more services (Phase 1, metric 1) | (a) this repo, `replay/inspect/`; (b) agent-eval-harness, a new branch | None (recorded logs; scripted model) | 1 day |
| **6. Overhead** | A timing script: MCP-backbone tool calls with tracing off, on with a live viewer, and on with a dead one; p50 and p95 | MCP-backbone branch, or here | None | 2 h |
| **7. Server and write-up** | A runbook for Arthur's server step; the README with screenshots and measured numbers; JOURNEY; a portfolio entry handed to the site session | This repo | None | 0.5 day |

**Progress (2026-10-28):** slices 0–3 and 6 are done, and slice 5(a): all
20 of the harness's published runs (1,777 samples, 28,884 spans) are
replayed with 0 problems on the fidelity, cost and token checks. 5(b)
waits for the gateway's tracing (slice 4).

**Progress (2026-10-29):** the end-to-end check (`e2e/check.py`) and a
local runner (`demos/end_to_end.py`) are built. A stand-in scripted agent
already runs agent -> gateway -> MCP-backbone -> Postgres in one unbroken
trace: the gateway forwards `_meta`, so MCP-backbone's spans nest under
the agent's. The check fails on the full path only because the gateway
has no spans of its own yet. Requested from their sessions: the gateway's
spans (slice 4), and the harness's tracer provider and scripted Pagila
agent (slice 5b), which replaces the stand-in.

**Slice 4 done (2026-10-29), on the gateway's branch:** with its tracing
(`feat/otel-tracing`, `26d58b4`), the full path passes the end-to-end
check: one trace, 37 spans from three services, every process stopped
normally. The first try lost the gateway's last spans on SIGTERM: uvicorn
re-raises the captured signal after a graceful shutdown, killing the
process before the gateway's `finally` ran. The gateway now turns SIGTERM
into an ordinary exit.

**Slice 5(b) done, and Phase 1's metric 1 met (2026-10-29), locally:**
the harness's own scripted Pagila agent (its `feat/otel-tracing`,
`1fc27ae`) answered task `e01` through the gateway (`caa9e65`) and
MCP-backbone (`main`) against the harness's local Pagila, graded a pass,
in one trace of 36 spans from three services; `e2e.check` passes its
default path, agent-eval-harness → mcp-security-gateway → mcp-backbone.
What remains is deployment (slice 7) and the write-up.

**Order:** spikes → 0 → 1 → 2 → 3 → checkpoint → (4, by the gateway
session) → 5 → 6 → 7. Slices 2 and 3 touch different repos and could run
in parallel.

## Fixed before running (so no choice is made after seeing results)

- **Fidelity (metric 3):** every recorded step of every traced run is in
  the viewer once, with the same parent, the same start (to the
  microsecond) and the same duration (to the millisecond). Anything less
  is a bug, investigated before the screenshot.
- **Cost (metric 2):** per run, the sum of model-call costs equals
  `pitstop.run.recorded_cost_usd` within $0.00001 per call (rounding of
  `NUMERIC(12, 6)`), for every run that recorded one.
- **No content by default (metric 4):** a test sends a query containing
  a unique marker string and asserts the marker appears in no attribute,
  status message or event of any span.
- **Whatever the overhead is gets published,** including "tracing adds
  more than expected".

## Spend plan

**Everything costs $0.** No step calls a paid model: Pitstop's model calls
are replayed from records, and slice 5's agent is scripted. The only
downloads are the Jaeger image (about 50 MB) and the OpenTelemetry Python
packages (a few MB).

## Fallback decision (made in advance, not under pressure)

If time runs short, cut in this order:
1. Slice 6's dead-viewer case (keep the off and on numbers).
2. The `rate_limit` and `authenticate` spans in MCP-backbone (keep
   `validate`, `cost_check`, `execute` and `audit`).
3. Replaying the backup runs (keep the daily and keyword-optimizer runs,
   which have the model calls).

**Never cut:**
- no content by default, proven by a test;
- tracing can never fail or stall a request, proven by a test;
- the recorded times, unshifted;
- the fidelity and cost checks;
- the private data staying out of git;
- the viewer bound to loopback.

**After v1 (tracked, not forgotten):**
- span metrics (latency and cost per tool and per model) through Jaeger's
  span-metrics connector, or Tempo if aggregation becomes a requirement;
- instrumenting Pitstop's Node code, if Pitstop ever runs again;
- a published fixture of replayed span data (no content), so others can
  load it;
- the gateway's audit log as span events.
- **Juku (Project 7) as a source.** It sends to this viewer under these
  conventions: one trace per request (app → gateway → MCP server →
  database) and one per weekly pipeline run, with the trace ID stored on
  its own per-call records. Juku owns its metrics, dashboards and alerts;
  all it needs from here is the viewer deployed on the server (slice 7)
  before it launches.
