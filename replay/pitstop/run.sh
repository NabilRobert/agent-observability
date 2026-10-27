#!/usr/bin/env bash
# Replays Pitstop's recorded traces into the local viewer, then checks them.
#
#   replay/pitstop/run.sh path/to/pitstop.dump [--include-content]
#
# Restores the dump into a throwaway Postgres container (no network, the
# dump mounted read-only), exports trace_events as JSON into the
# git-ignored replay/pitstop/out/, removes the container, sends the spans
# over OTLP and checks each run against the viewer. Nothing touches a live
# database. The viewer must be running (viewer/compose.yaml).
set -euo pipefail

DUMP="${1:?usage: run.sh path/to/pitstop.dump [--include-content]}"
shift
DUMP="$(cd "$(dirname "$DUMP")" && pwd)/$(basename "$DUMP")"
cd "$(dirname "$0")/../.."
OUT=replay/pitstop/out
# postgres:16, pinned (the snapshot came from a 16.x server); the same
# image as the Pitstop case study's evidence script.
IMAGE="postgres:16@sha256:9e2cf6287f25c3151f4c9421ccce2b17f3e60061c111a29165e0d2cdd9ff0a97"
NAME="agentobs-pitstop-$$"

[ -f "$DUMP" ] || { echo "error: no dump at $DUMP" >&2; exit 2; }
if command -v sha256sum >/dev/null; then SHA=$(sha256sum "$DUMP" | cut -d' ' -f1); else SHA=$(shasum -a 256 "$DUMP" | cut -d' ' -f1); fi

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$NAME" --network none -e POSTGRES_PASSWORD=replay -e POSTGRES_DB=pitstop \
  -v "$DUMP:/snapshot.dump:ro" -v "$PWD/replay/pitstop/export.sql:/export.sql:ro" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$NAME" pg_isready -U postgres -d pitstop -q 2>/dev/null && break
  sleep 1
done
sleep 1
docker exec "$NAME" pg_restore -U postgres -d pitstop --no-owner --no-privileges --exit-on-error /snapshot.dump

mkdir -p "$OUT"
docker exec "$NAME" psql -U postgres -d pitstop -X -q -At -v ON_ERROR_STOP=1 -f /export.sql > "$OUT/steps.json"
cleanup
echo "exported $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$OUT/steps.json") steps from $(basename "$DUMP") (sha256 ${SHA:0:12}…)"

uv run python -m replay.pitstop.replay send "$OUT/steps.json" --snapshot "$(basename "$DUMP")" "$@"
sleep 3 # the viewer batches writes
uv run python -m replay.pitstop.replay check "$OUT/steps.json"
