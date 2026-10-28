# Tracing conventions

The one mapping every source follows (Phase 5, ADR-004). Where an
OpenTelemetry convention covers a case, it is used as written; this doc
only records which ones, and fills the gaps.

**Revisions followed:**
- GenAI and MCP conventions:
  [`open-telemetry/semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai)
  at `e19717d65f89` (2026-10-27). Status: Development, so names may still
  change; when they do, this doc and the sources move together.
- Database, error and RPC attributes: OpenTelemetry semantic conventions
  v1.44.0, the version the GenAI conventions link to.

## Rules for every source

1. **Resource:** `service.name` (for example `mcp-backbone`, `pitstop`) and
   `service.version` (the build's git version when known).
2. **Trace context** crosses MCP hops in `params._meta` (`traceparent`,
   `tracestate`), unprefixed, and other HTTP hops in the `traceparent`
   header. A proxy extracts on the way in and injects from inside its own
   span on the way out.
3. **Errors:** the span status is `ERROR`, with `error.type` set to a
   short, stable value (an exception class, a code, or a category such as
   `rejected_by_cost`). The status message is free text, so it counts as
   content (rule 5).
4. **Cost:** `agentobs.cost.usd` (double, US dollars) on the span that
   incurred it. Never rolled up into a parent. Absent, not `0`, when the
   cost is unknown.
5. **Content is opt-in.** These are never recorded unless the source's
   content-capture setting is on: SQL text, prompts and responses, tool
   arguments and results, free-text summaries, reasons and error messages
   that can quote input. Names, counts, timings, models, tokens, costs and
   outcomes are not content.
6. **Own attributes** use the source's namespace: `mcp_backbone.*`,
   `pitstop.*`, `<gateway>.*`. This project's shared ones use `agentobs.*`.

## Span types

| Work | Span name | Kind | Key attributes |
|---|---|---|---|
| Model call | `chat {gen_ai.request.model}` (just `chat` when the model is unknown) | CLIENT | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.reasoning.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.request.max_tokens`, `gen_ai.request.reasoning.level`, `gen_ai.output.type`, `agentobs.cost.usd` |
| Agent | `invoke_agent {gen_ai.agent.name}` | INTERNAL (in-process) or CLIENT (remote agent) | `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name` |
| Workflow (a pipeline of agents) | `invoke_workflow {gen_ai.workflow.name}` | INTERNAL | `gen_ai.operation.name=invoke_workflow`, `gen_ai.workflow.name` |
| Tool call, agent side | `execute_tool {gen_ai.tool.name}` | INTERNAL | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| MCP request, client and server | Server: `{mcp.method.name} {target}`, e.g. `tools/call run_query`. Client: the convention says the same, but MCP SDK 2.3.0 names it `MCP send {mcp.method.name} {target}` (with only `mcp.method.name` and `jsonrpc.request.id`); left as the SDK makes it | CLIENT / SERVER | `mcp.method.name`, `mcp.protocol.version`, `mcp.session.id`, `jsonrpc.request.id`, `gen_ai.tool.name`, `gen_ai.operation.name=execute_tool` (for `tools/call`). **The MCP Python SDK 2.x emits these itself**; a source only installs a tracer provider |
| Database call | the stage name (see below) | CLIENT | `db.system.name=postgresql`, `db.operation.name`, `db.collection.name` (when it is our own table), `db.response.returned_rows`; `db.query.text` only with content capture on |

## MCP-backbone

Under the SDK's `tools/call <tool>` server span. Content capture:
`APP_OTEL_CAPTURE_CONTENT=true` (off by default).

| Span | Kind | Attributes (beyond the table above) |
|---|---|---|
| `authenticate` | INTERNAL | `error.type` on refusal: the exception's class (`AuthenticationError`, `ToolNotPermitted`) |
| `rate_limit` | INTERNAL | `error.type=rate_limited` when refused |
| `validate` | INTERNAL | `error.type=rejected_by_validation` when refused |
| `cost_check` | CLIENT | `db.operation.name=EXPLAIN`, `mcp_backbone.cost.estimate`, `mcp_backbone.cost.threshold`; `error.type=rejected_by_cost` when refused, or the exception's class when `EXPLAIN` itself fails |
| `execute` | CLIENT | `db.operation.name=SELECT`, `db.response.returned_rows`, `mcp_backbone.result.truncated`, `mcp_backbone.execute.attempts`; `error.type` = `timed_out` or `errored` |
| `audit` | CLIENT | `db.operation.name=INSERT`, `db.collection.name=query_executions`; the exception's class if the write fails (the call then fails too) |

On the `tools/call` span: `mcp_backbone.query.status` (`succeeded`,
`rejected`, `errored`, `timed_out`). With capture on: `db.query.text` on
`execute`, and the rejection reason or exception message as the status
message. The SDK's own exception recording is off on these spans: it
would copy the message, which can quote the query, into the span.

## Pitstop (replayed)

Resource: `service.name=pitstop`, `agentobs.replay=true`,
`agentobs.replay.snapshot` (the backup's file name). One trace per
pipeline run; IDs derived from the run and step IDs. Every span carries
`pitstop.step.kind` and `pitstop.step.id`; the root also carries
`pitstop.run.id`, `pitstop.run.type` and, when recorded,
`pitstop.run.recorded_cost_usd`.

| Recorded step (`kind`) | Span name | Kind | Mapping |
|---|---|---|---|
| `run` (daily, keyword optimizer) | `invoke_workflow {run_type}` | INTERNAL | `gen_ai.workflow.name={run_type}` |
| `run` (backup) | `run backup` | INTERNAL | No GenAI attributes: it calls no model |
| `agent` | `invoke_agent {name}` | INTERNAL | `gen_ai.agent.name={name}`; `pitstop.agent.type` from `meta.agent_type` |
| `critic_review` | `invoke_agent Ciri (critic)` | INTERNAL | `gen_ai.agent.name=Ciri (critic)`; `pitstop.step.name` (e.g. "Ciri reviews analysis (pass 1)"), `pitstop.critic.pass`, `pitstop.critic.reviewed_agent`, `pitstop.critic.grade` |
| `llm_call` | `chat {model}` | CLIENT | `gen_ai.provider.name=openai` (an OpenAI-compatible API), tokens in and out, `meta.cached_tokens` → `gen_ai.usage.cache_read.input_tokens`, `meta.reasoning_tokens` → `gen_ai.usage.reasoning.output_tokens`, `meta.finish_reason` → `gen_ai.response.finish_reasons`, `meta.max_completion_tokens` → `gen_ai.request.max_tokens`, `meta.reasoning_effort` → `gen_ai.request.reasoning.level`, `meta.json` → `gen_ai.output.type` (`json` or `text`), `cost_usd` → `agentobs.cost.usd`, `meta.attempt` → `pitstop.llm.attempt` |
| `context`, `decision`, `revision`, `email`, `error` | the recorded name | INTERNAL | Numeric and boolean `meta` values as `pitstop.meta.<key>`; string values are content |
| any, `status=error` | | | Span status `ERROR`, `error.type=pitstop.{kind}_error`; the error message only with capture on |
| any, `status=skipped` | | | Status unset; `pitstop.step.status=skipped` |

Never read: `trace_payloads` (prompts, responses, built context). With
`--include-content`, the replay adds the short `summary`, string `meta`
values and error messages; it never adds payloads.

## agent-eval-harness (replayed from its Inspect logs)

Resource: `service.name=agent-eval-harness`, `agentobs.replay=true`,
`agentobs.replay.snapshot` (the harness repo's commit and the run, e.g.
`agent-eval-harness@14cf126b5d21 runs/proof-mini-a`). One trace per
sample and epoch; IDs derived from Inspect's own span and event IDs.
Inspect's own span tree is kept as it was recorded.

| Recorded | Span name | Kind | Mapping |
|---|---|---|---|
| The sample | `eval sample {id}` | INTERNAL | `inspect.run`, `inspect.eval_id`, `inspect.task`, `inspect.sample.id`, `inspect.epoch`, `inspect.model`, `gen_ai.provider.name`, `gen_ai.request.model`; scores as `inspect.score.{scorer}.{key}`; a hit limit as `inspect.limit.type` and `inspect.limit.value`; the sample's own totals as `inspect.recorded.cost_usd`, `.input_tokens`, `.output_tokens` (the check compares them with the calls) |
| Span, type `agent` | `invoke_agent {name}` | INTERNAL | `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name` |
| Span, type `tool` (with its tool event) | `execute_tool {name}` | INTERNAL | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.type=function`, `gen_ai.tool.call.id`; a tool error as `error.type=inspect.tool_{type}` |
| Other spans (`init`, `solvers`, `solver`, `scorers`, `scorer`) | `{type} {name}`, or the name when they match | INTERNAL | `inspect.span.type` |
| Model event | `chat {model}` | CLIENT | `gen_ai.provider.name` (`openai` for `openai-api/...`), `gen_ai.request.model` (the last path segment), `inspect.model` (in full), input, output, cache-read and reasoning tokens, `gen_ai.response.finish_reasons`, `gen_ai.response.id`, `agentobs.cost.usd` from the call's own `total_cost`; a failed call as `error.type` (the exception's class) |
| A model call with no recorded end (cancelled) | as above | CLIENT | zero length, `inspect.event.unfinished=true` |

Never read: prompts, model outputs, tool results. With
`--include-content`, the replay adds tool-call arguments
(`gen_ai.tool.call.arguments`) and error messages.

## For the gateway

- Let the MCP SDK's own server and client spans stand; call the upstream
  from inside the gateway's handling span, so the SDK injects the context
  into the upstream request's `_meta`.
- Pass the rest of `params._meta` through unchanged. The trace context is
  only one of its keys: Juku (Project 7) puts a signed delegation token
  there too, and both have to reach the MCP server intact.
- Name its own steps as plain stages under the server span (for example
  `authorize`, `policy`, `detect`), with `error.type` for denials and
  blocks.
- Tool arguments and outputs are content (rule 5). Detector verdicts,
  scores, rule IDs and timings are not.
