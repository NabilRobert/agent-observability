#!/usr/bin/env bash
# Smoke test for the viewer with nothing but curl (for a server without
# uv): sends one span as OTLP/JSON and reads it back from the query API.
#
#   viewer/smoke.sh            (defaults: 127.0.0.1, the compose file's ports)
set -euo pipefail

OTLP="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}"
QUERY="${JAEGER_QUERY_URL:-http://127.0.0.1:16686}"
TRACE_ID=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
SPAN_ID=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
NOW_NS="$(date +%s)000000000"

curl -sf -o /dev/null -X POST "$OTLP/v1/traces" -H 'Content-Type: application/json' -d @- <<JSON
{"resourceSpans": [{
  "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "viewer-smoke"}}]},
  "scopeSpans": [{"spans": [{
    "traceId": "$TRACE_ID", "spanId": "$SPAN_ID", "name": "smoke", "kind": 1,
    "startTimeUnixNano": "$NOW_NS", "endTimeUnixNano": "$NOW_NS"
  }]}]
}]}
JSON

for _ in $(seq 1 20); do
  if curl -sf "$QUERY/api/v3/traces/$TRACE_ID" | grep -q '"name":"smoke"'; then
    echo "ok: trace $TRACE_ID stored and read back"
    exit 0
  fi
  sleep 0.5
done
echo "failed: trace $TRACE_ID not readable from $QUERY" >&2
exit 1
