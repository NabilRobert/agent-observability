# Running everything

The [README](../README.md#run-it) starts the viewer. Everything below
needs it running. (`viewer/smoke.sh` checks it with only `curl`.)

The viewer is Jaeger v2 in one container, every port on `127.0.0.1`:

- UI: http://127.0.0.1:16686 (no login)
- OTLP/HTTP in: http://127.0.0.1:4318 (a source's
  `OTEL_EXPORTER_OTLP_ENDPOINT`)
- Query API: `/api/v3/...` (Jaeger 2.22 serves only v3)

## The end-to-end trace

Run the whole path locally, then check the trace (needs an MCP-backbone
checkout and the mcp-gateway repo; the gateway is exported with `git
archive`, so its checkout isn't touched):

```bash
uv run --project ../MCP-backbone python demos/end_to_end.py --backbone ../MCP-backbone --gateway ../mcp-gateway
```

```bash
uv run python -m e2e.check --service demo-agent --service mcp-security-gateway --service mcp-backbone
```

With the harness's own agent instead (needs an agent-eval-harness
checkout with tracing, and its local Pagila stack up), the check runs on
its default path:

```bash
uv run --project ../MCP-backbone python demos/end_to_end.py --backbone ../MCP-backbone --gateway ../mcp-gateway --agent harness --harness ../agent-eval-harness
```

```bash
uv run python -m e2e.check
```

The check passes when the latest trace has one root, no span whose parent
is missing, every service on the path, and a database stage reached
through them in order.

## The harness replay

Replay the harness's published runs (needs a clone of agent-eval-harness;
read with `git show`, so its checkout isn't touched):

```bash
replay/inspect/run.sh path/to/agent-eval-harness
```

## Tracing MCP-backbone

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` in its
environment. The cross-process demo is
[`demos/mcp_backbone_trace.py`](../demos/mcp_backbone_trace.py).

## Tests

```bash
uv run pytest
```
