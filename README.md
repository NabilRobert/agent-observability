# agent-observability

One tracing pipeline across a set of agent projects: they emit OpenTelemetry
traces, following the GenAI semantic conventions, into one small,
self-hosted trace viewer, so a single agent request can be followed from the
agent through an MCP gateway and an MCP server to the database, with latency
and cost at every step.

Work in progress. The plan is in [`docs/planning/`](docs/planning/).
