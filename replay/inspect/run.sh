#!/usr/bin/env bash
# Replays the harness's published Inspect runs into the local viewer, then
# checks them.
#
#   replay/inspect/run.sh path/to/agent-eval-harness [git-ref] [--include-content]
#
# Reads every runs/*/inspect.eval at the ref (default: origin/main) with
# `git show`, so the harness's working tree and branches are never
# touched. The logs are already public in the harness repo; the copies
# go into the git-ignored replay/inspect/out/. The viewer must be running.
set -euo pipefail

HARNESS="$(cd "${1:?usage: run.sh path/to/agent-eval-harness [git-ref] [--include-content]}" && pwd)"
REF="${2:-origin/main}"
cd "$(dirname "$0")/../.."
OUT=replay/inspect/out
SHA=$(git -C "$HARNESS" rev-parse --short=12 "$REF")

rm -rf "$OUT" && mkdir -p "$OUT"
files=()
while IFS= read -r path; do
  run=$(basename "$(dirname "$path")")
  git -C "$HARNESS" show "$REF:$path" > "$OUT/$run.eval"
  files+=("$OUT/$run.eval")
done < <(git -C "$HARNESS" ls-tree -r --name-only "$REF" -- runs | grep '/inspect\.eval$')
[ ${#files[@]} -gt 0 ] || { echo "error: no runs/*/inspect.eval at $REF" >&2; exit 2; }
echo "extracted ${#files[@]} runs from the harness at $SHA"

uv run python -m replay.inspect.replay send "${files[@]}" --snapshot "agent-eval-harness@$SHA" "${@:3}"
sleep 3 # the viewer batches writes
uv run python -m replay.inspect.replay check "${files[@]}"
