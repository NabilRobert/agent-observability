# The trace viewer

Jaeger v2 in one container (Phase 5, ADR-001): OTLP over HTTP in, traces
kept in memory (at most 5,000), UI and query API out. Every port is bound
to `127.0.0.1`; the UI has no login (ADR-007). A restart empties it; the
replays put their traces back in seconds.

```bash
docker compose -f viewer/compose.yaml up -d
```

```bash
uv run python viewer/smoke.py
```

(`viewer/smoke.sh` does the same with only `curl`, for a server without
`uv`.)

- UI: http://127.0.0.1:16686
- OTLP/HTTP: http://127.0.0.1:4318 (set `OTEL_EXPORTER_OTLP_ENDPOINT` to
  this in a source)
- Query API: `/api/v3/...` (Jaeger 2.22 serves only v3)

Limits: 256 MB of memory and half a CPU. Measured: 79 MiB holding both
replays (29,500 spans). Badger, Jaeger's disk store, was the first choice
and peaked at 532 MiB importing the same data: see the as-built note on
ADR-001.

On the shared server, the UI is reached only through an SSH tunnel: see
[RUNBOOK.md](RUNBOOK.md) for installing, feeding, operating and removing it.
