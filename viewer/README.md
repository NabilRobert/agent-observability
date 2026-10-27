# The trace viewer

Jaeger v2 in one container (Phase 5, ADR-001): OTLP over HTTP in, traces
kept in Badger on a named volume for 30 days, UI and query API out. Every
port is bound to `127.0.0.1`; the UI has no login (ADR-007).

```bash
docker compose -f viewer/compose.yaml up -d
```

```bash
uv run python viewer/smoke.py
```

- UI: http://127.0.0.1:16686
- OTLP/HTTP: http://127.0.0.1:4318 (set `OTEL_EXPORTER_OTLP_ENDPOINT` to
  this in a source)
- Query API: `/api/v3/...` (Jaeger 2.22 serves only v3)

Limits: 256 MB of memory and half a CPU. Measured idle: 15 MiB
(Phase 6, Spike 1). Badger preallocates a sparse 2.3 GB value log; real
disk use starts at a few KB.

On a server, the UI is reached through an SSH tunnel to port 16686.
