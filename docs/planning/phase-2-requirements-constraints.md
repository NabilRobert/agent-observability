# Phase 2: Requirements & Constraints

**Time budget:** short, in parallel with the gateway work, but every
planning phase is written out (Phases 1–7), as for the gateway. The build
goes in slices (Phase 7).

## Core scenario

1. Arthur opens the trace viewer through an SSH tunnel (or locally) and
   searches by service: `mcp-backbone`, `pitstop`, and later the gateway
   and the harness.
2. He opens an MCP-backbone `run_query` call. He sees how long each stage
   took (authenticate, rate limit, validate, cost check, execute, audit),
   the cost estimate and the outcome, but no SQL text.
3. He opens a replayed Pitstop daily run. He sees the run → agents →
   critic reviews → model calls tree with the original timings, plus tokens
   and cost on every model call.
4. Later: he runs one harness sample against the gateway. One trace shows
   the agent's turns, the model calls, the tool call crossing the gateway,
   and MCP-backbone's stages beneath it.
5. A reviewer sees the same through the README's screenshots and the
   conventions doc.

## Requirements

**Viewer**
- **V1.** One container, pinned by digest, started with one command
  (`docker compose up -d`). It receives OTLP over HTTP on port 4318.
- **V2.** A memory limit of 256 MB, and storage on disk with a retention
  period, so neither memory nor disk grows without bound.
- **V3.** All ports bound to loopback. The UI is never exposed publicly
  without authentication.

**Conventions** (written once in `docs/conventions.md`, followed by every
source)
- **C1.** Span names and attributes follow the OpenTelemetry GenAI
  conventions for model, agent and tool spans, the MCP conventions for MCP
  spans, and the database conventions for database spans. The doc records
  which revision was followed, since these conventions are still at
  "Development" status.
- **C2.** Trace context crosses every MCP hop in the request's
  `params._meta` (`traceparent`, `tracestate`), as the MCP conventions and
  SEP-414 specify. Non-MCP HTTP hops use the standard `traceparent` header.
- **C3.** Anything not in the conventions uses a namespaced attribute.
  Cost, which the conventions don't define, is one agreed attribute
  everywhere.
- **C4.** Content (SQL text, prompts, responses, free-text summaries) is
  recorded only behind an explicit opt-in, off by default.

**Sources**
- **S1. MCP-backbone:** a span per stage under the SDK's own span per tool
  call, on its own branch, with tests.
- **S2. Pitstop:** a replay of the recorded `trace_events` from a local
  restore of the backup into OTLP, with the original timestamps.
- **S3. The gateway:** instrumented by the gateway's own session, following
  `docs/conventions.md`; it must pass `_meta` trace context through.
- **S4. The harness:** joins once its in-flight branch is merged.

**Safety**
- **R1.** A tracing failure never fails or stalls a request: the export is
  batched in the background with a bounded queue, and errors are dropped.
- **R2.** Tracing is off unless an endpoint is configured. With it off, no
  export connection is ever made.

**Tests**
- **T1.** Every repo touched keeps its full test suite and linters
  passing. For MCP-backbone: ruff, pyright and pytest against the test
  Postgres container.
- **T2.** New tests cover: the span tree's shape; no SQL text by default;
  trace context arriving through `_meta` is honoured; a dead exporter
  changes nothing. The replay's mapping is tested on a synthetic fixture,
  never on the private data. New tests are mutation-checked.

## Non-functional

- **Footprint:** the viewer shares a small server with MCP-backbone and
  the Pitstop demo, so memory is capped (V2) and measured.
- **Overhead:** measured on MCP-backbone (Phase 1, metric 6), not assumed.
- **Privacy:** C4 and R2; the backup and anything exported from it stay
  git-ignored.
- **A reviewer gets it in about 15 minutes** (Phase 1, metric 8).

## Constraints

- **$0 model budget.** No paid model calls of any kind.
- **Parallel work:** the gateway repo and the harness (and its worktrees)
  are off limits during this pass. The gateway session builds the gateway's
  tracing; the harness joins after its branch is merged.
- **MCP-backbone:** the work goes on a new branch off an up-to-date
  `main`, in a separate worktree, so Arthur's checkout and his uncommitted
  change there are left alone.
- **Server work goes through Arthur.** Public docs carry no server
  addresses.
- **The MCP Python SDK (2.3.0) already emits MCP spans** and propagates
  context through `_meta`. Our work installs a tracer provider and adds
  child spans; it doesn't re-create what the SDK does.
- **Commit, never push.** Arthur pushes.

## Must / Should / Could

| | Item |
|---|---|
| **Must** | V1–V3: the viewer, running locally, pinned and capped |
| **Must** | C1–C4: `docs/conventions.md` |
| **Must** | S1: MCP-backbone's stage spans, with the T2 tests, on a branch |
| **Must** | S2: the Pitstop replay, with a fidelity check (Phase 1, metric 3) and the cost check (metric 2) |
| **Must** | R1, R2 |
| **Must** | A screenshot of each source in the viewer |
| **Should** | The overhead measurement (metric 6) |
| **Should** | S3 and S4, the end-to-end trace (metric 1). These depend on other sessions' work |
| **Should** | The server deploy, done by Arthur from a written runbook |
| **Could** | A published fixture of replayed span data (names, timings, tokens and costs only), so others can load it |
| **Could** | Metrics derived from spans (latency and cost per tool or model) |
| **Won't** | New model calls, a public viewer, logs or metrics pipelines, changes to Pitstop's code |

## Rough effort

About 2 days of work, plus waiting on the other sessions:
- planning and conventions: a quarter of a day;
- the viewer: an hour;
- MCP-backbone: half a day, mostly tests;
- the Pitstop replay: half a day;
- the end-to-end trace and the write-up: half a day, once the gateway and
  the harness are ready.
