# Planning trail

Written before the build, in order. Each phase builds on the previous one.
They're kept as they were written, as a record of what was planned and why.
**Archive** phases are left as written; **live** ones carry "as built" notes
added in place where the build changed or settled something. For what was
built, start with the [README](../../README.md) and
[`conventions.md`](../conventions.md).

| Phase | What it settles | Status |
|---|---|---|
| [1. Problem brief](phase-1-problem-brief.md) | The problem, who it's for, success metrics, non-goals | Archive |
| [2. Requirements and constraints](phase-2-requirements-constraints.md) | The core scenario, requirements, must / should / could, constraints | Archive |
| [3. Domain model](phase-3-domain-model.md) | The terms, the span trees per source, the invariants | Archive; the span trees as built are in [`conventions.md`](../conventions.md) |
| [4. Flows and failures](phase-4-flows-failures.md) | Each flow step by step, and what happens when each step fails | Archive |
| [5. Architecture and decisions](phase-5-architecture-decisions.md) | The ADRs: each decision, its alternatives and consequences | Live |
| [6. Risks and skeleton](phase-6-risks-skeleton.md) | The riskiest assumptions, tested with spikes before building | Archive, plus the spike results |
| [7. Slices and plan](phase-7-slices-plan.md) | The work cut into vertical slices, with a fallback decided in advance | Live |

## Decisions (ADRs)

All in [Phase 5](phase-5-architecture-decisions.md), each with the
options it beat and its consequences. Where the build changed one, an
"as built" note is added under it rather than rewriting it.

| ADR | Decision |
|---|---|
| [001](phase-5-architecture-decisions.md#adr-001-the-viewer-is-jaeger-v2-in-one-container-with-badger-storage) | The viewer is Jaeger v2, in one container (as built: traces kept in memory, not in Badger) |
| [002](phase-5-architecture-decisions.md#adr-002-export-straight-to-the-viewer-over-otlphttp) | Export straight to the viewer over OTLP/HTTP |
| [003](phase-5-architecture-decisions.md#adr-003-trace-context-crosses-mcp-in-params_meta) | Trace context crosses MCP in `params._meta` |
| [004](phase-5-architecture-decisions.md#adr-004-one-attribute-mapping-written-down-once) | One attribute mapping, written down once |
| [005](phase-5-architecture-decisions.md#adr-005-mcp-backbone-gets-hand-written-spans-not-auto-instrumentation) | MCP-backbone gets hand-written spans, not auto-instrumentation |
| [006](phase-5-architecture-decisions.md#adr-006-pitstop-is-replayed-from-its-records-not-re-run) | Pitstop is replayed from its records, not re-run |
| [007](phase-5-architecture-decisions.md#adr-007-on-the-server-the-viewer-is-reachable-only-by-ssh-tunnel) | On the server, the viewer is reachable only by SSH tunnel |
| [008](phase-5-architecture-decisions.md#adr-008-this-repo-is-python-with-the-same-tooling-as-mcp-backbone) | This repo is Python, with the same tooling as MCP-backbone |
| [009](phase-5-architecture-decisions.md#adr-009-the-live-end-to-end-run-reuses-the-harnesss-own-scripted-agent) | The live end-to-end run reuses the harness's own scripted agent |

## Spikes

The four Phase 6 spikes, their results and what each changed in the
build are in [Phase 6, "Spike results"](phase-6-risks-skeleton.md#spike-results).
Their throwaway scripts are on the `spike/phase-6` branch.

## Related

- The attribute mapping every source follows:
  [`../conventions.md`](../conventions.md).
- The measured tracing overhead:
  [`../mcp-backbone-overhead.md`](../mcp-backbone-overhead.md).
