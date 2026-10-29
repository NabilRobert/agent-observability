# Runbook: the trace viewer on the shared server

How to install, reach, feed, run and remove the viewer (Jaeger v2, one
container) on the server that also runs MCP-backbone. Decisions behind it:
[Phase 5](../docs/planning/phase-5-architecture-decisions.md), ADR-001
(Jaeger, bounded in-memory storage, 256 MB) and ADR-007 (reachable only
by SSH tunnel).

`<server>` and `<user>` stand for the server's SSH host and login; they
are not written down here on purpose. Commands marked *laptop* run on your
own machine; all others run on the server. To run the viewer locally
instead, see "Run it" in the [README](../README.md#run-it).

## What runs

| Piece | Detail |
|---|---|
| Container | `agent-observability-jaeger-1`, from `viewer/compose.yaml` |
| Image | `jaegertracing/jaeger` 2.22.0, pinned by its multi-arch digest (amd64 and arm64) |
| Ports | 16686 (UI and query API), 4318 (OTLP/HTTP in), 13133 (health): **all on 127.0.0.1 only** |
| Limits | 256 MB of memory, half a CPU, read-only root filesystem, no capabilities |
| Storage | In memory, at most 5,000 traces (the oldest are dropped first); nothing on disk |
| Measured | 79 MiB holding both replays (Pitstop's 62 runs and the harness's 1,777 samples, 29,500 spans); peak 73–79 MiB while importing them |
| Secrets | None. The UI has no login, which is why nothing is published |

Traces are a view, not a record: each system's own records (MCP-backbone's
audit table, Pitstop's backup, the harness's run logs) are the truth. So
the viewer keeps traces in memory only, and **a restart empties it**: re-run
the two replays below (they take seconds, and give the same IDs every
time), and live traces start again from the next tool call.

Why not on disk: Jaeger's disk store (Badger) peaked at 532 MiB importing
the harness's runs, and Jaeger offers no way to size its buffers, so it
was killed at the 256 MB limit. The same traces in memory take under
80 MiB.

## Before installing (once)

```bash
docker compose version
```

```bash
free -m
```

The `available` column should show at least 300 MB: the 256 MB limit plus
a margin, on a server shared with other projects.

```bash
ss -ltn | grep -E ':(16686|4318|13133)\b'
```

This should print nothing: the three ports must be free.

## Install

Clone the repo (once it's on GitHub; until then, copy the `viewer/`
folder over with `scp`), start the container, and check it end to end:

```bash
git clone https://github.com/NabilRobert/agent-observability.git ~/agent-observability
```

```bash
cd ~/agent-observability && docker compose -f viewer/compose.yaml up -d
```

```bash
~/agent-observability/viewer/smoke.sh
```

`smoke.sh` needs only `curl`: it sends one span and reads it back. It
prints `ok: trace ... stored and read back`.

## Check it's private

```bash
ss -ltnp | grep -E ':(16686|4318|13133)\b'
```

Every line must show `127.0.0.1`, never `0.0.0.0` or `*`. This matters
more than the firewall: Docker's own iptables rules bypass ufw, so a port a
container publishes on all interfaces is reachable whatever ufw says. Then,
from your *laptop*, this must fail (time out or be refused):

```bash
curl -m 5 http://<server>:16686
```

## Open the UI (laptop)

```bash
ssh -N -L 16686:127.0.0.1:16686 <user>@<server>
```

Then open http://127.0.0.1:16686. If a local viewer already uses 16686 on
the laptop, forward another local port instead (`-L 26686:127.0.0.1:16686`,
then http://127.0.0.1:26686). Stop the tunnel with Ctrl-C.

## Send MCP-backbone's traces

Needs an MCP-backbone build with tracing (its `main` has it since the
tracing PR merged), deployed with the steps in its own `deploy/RUNBOOK.md`.

MCP-backbone reads the endpoint from its **process environment**, not from
its `.env` file (that file only supplies `APP_*` settings), so it goes in a
systemd drop-in:

```bash
sudo systemctl edit mcp-backbone
```

Add these two lines in the editor that opens, then save:

```ini
[Service]
Environment=OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

```bash
sudo systemctl restart mcp-backbone && systemctl is-active mcp-backbone
```

Make one tool call (for example `list_tables` through the plugin), then
search for the service `mcp-backbone` in the UI.

**Never set `APP_OTEL_CAPTURE_CONTENT` on the server.** It is off by
default, and with it off no SQL text or rejection reason reaches a span.

## Send Pitstop's replayed traces (laptop)

The backup never leaves the laptop: the replay runs there and sends spans
through a tunnel. Forward both the OTLP port (to send) and the query port
(for the checks), on local ports that don't clash with a local viewer:

```bash
ssh -N -L 14318:127.0.0.1:4318 -L 26686:127.0.0.1:16686 <user>@<server>
```

In a second terminal, from the repo:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:14318 JAEGER_QUERY_URL=http://127.0.0.1:26686 replay/pitstop/run.sh path/to/pitstop.dump
```

It ends with `ok: 62 runs, 653 steps checked against the viewer`. The IDs
are derived from the records, so running it again changes nothing.

## Send the harness's replayed runs (laptop)

The harness's run logs are already public in its repo; the replay reads
them from its git history with `git show`, so its checkout is untouched.
With the same tunnel open:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:14318 JAEGER_QUERY_URL=http://127.0.0.1:26686 replay/inspect/run.sh path/to/agent-eval-harness
```

It ends with `ok: 20 runs, 1777 samples, 28884 spans checked against the
viewer, with cost and tokens; 0 problems` (at the harness's `main` of
2026-10-28; later runs add more).

## Routine operations

All from `~/agent-observability`.

| Task | Command |
|---|---|
| Is it up? | `docker compose -f viewer/compose.yaml ps` and `curl -s http://127.0.0.1:13133/status` |
| End-to-end check | `viewer/smoke.sh` |
| Logs | `docker compose -f viewer/compose.yaml logs --tail 50` |
| Memory | `docker stats --no-stream agent-observability-jaeger-1` |
| Restart (**empties it**; re-run the replays) | `docker compose -f viewer/compose.yaml restart` |

**Change how much it keeps:** edit `max_traces` in `viewer/config.yaml`
(commit the change), then restart. 1,839 traces took about 75 MiB, so
memory grows by roughly 40 KiB per trace of the harness's size; keep
the limit well inside the 256 MB.

**Upgrade Jaeger:**
1. On the *laptop*, pick a release at least two weeks old.
2. Get its multi-arch digest with
   `docker buildx imagetools inspect jaegertracing/jaeger:<version>` (the
   top-level `Digest`).
3. Put the digest in `viewer/compose.yaml`, run `viewer/smoke.py` locally,
   and commit.
4. On the server, run `git pull`, then
   `docker compose -f viewer/compose.yaml up -d`, then `viewer/smoke.sh`.

## When something's wrong

| Symptom | Check | Fix |
|---|---|---|
| UI unreachable through the tunnel | `ssh` printed `bind: Address already in use`? `docker compose ... ps` on the server | Use another local port; start the container |
| No MCP-backbone traces | `sudo cat /proc/$(systemctl show -p MainPID --value mcp-backbone)/environ \| tr '\0' '\n' \| grep OTEL` shows the endpoint? `viewer/smoke.sh` passes? Does the deployed commit have `app/tracing.py`? | Add the drop-in and restart; start the viewer; deploy the tracing branch |
| A Pitstop run doesn't show up in search | The runs are dated 28 Sep to 25 Oct 2026 | Widen the search's lookback, or look it up by trace ID |
| The container keeps restarting | `docker inspect -f '{{.State.OOMKilled}}' agent-observability-jaeger-1` | `true`: shorten the retention, or raise `mem_limit` after checking `free -m` |
| MCP-backbone restarts slowly | `journalctl -u mcp-backbone` for `tracing: gave up flushing spans` | Nothing to do: a dead viewer costs at most 2 s per shutdown, by design |

A dead or stopped viewer never breaks MCP-backbone: tool calls carry on,
and spans are dropped until the viewer is back.

## Remove it

1. Stop MCP-backbone sending: `sudo systemctl edit mcp-backbone`, delete
   the `Environment=OTEL_EXPORTER_OTLP_ENDPOINT=...` line, save, then
   `sudo systemctl restart mcp-backbone`. (This step is optional for
   safety, since a dead viewer is harmless, but it keeps the logs quiet.)
2. Stop and remove the viewer:
   `docker compose -f viewer/compose.yaml down -v`. The traces were only in
   memory, so they go with it (replayed ones can be replayed again; live
   ones are gone); `-v` also removes the empty `/tmp` volume the image
   declares.
