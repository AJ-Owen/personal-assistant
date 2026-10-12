---
description: Launch and interact with the personal assistant stack (FastAPI + MCP sidecars + Obsidian)
---

# Run skill — personal assistant

This is a Docker Compose stack. All services run in containers; there is no local Python venv.

## Prerequisites

- Docker running
- `.env` file present with at minimum `ANTHROPIC_API_KEY`, `TODOIST_API_KEY`, `BRAVE_API_KEY`
- `TZ=Europe/London` must be set in `.env` for scheduling to fire at the right local time

## Start the stack

```bash
docker compose up --build -d
```

Wait for startup (~10s), then verify:

```bash
docker compose ps
# All services should show "running"
# Expected: assistant, todoist-mcp, brave-search-mcp, obsidian-mcp, obsidian-remote
```

Check the assistant came up cleanly:

```bash
docker compose logs assistant --tail=20
# Look for: "Application startup complete"
# Look for: "MCP connected http://todoist-mcp:3000/mcp"
# Look for: "MCP connected http://brave-search-mcp:3001/mcp"
# Look for: "MCP connected http://obsidian-mcp:3002/mcp"
# Look for: "Scheduler started — inbox processing at 06:00 Europe/London, daily summary at 07:00 Europe/London"
```

## Deploy a code change

**App source is baked into the image — there is no source mount and no auto-reload.**
The running container keeps executing old code until you rebuild:

```bash
docker compose up -d --build assistant
until docker compose logs assistant --since 2m 2>&1 | grep -q "Application startup complete"; do sleep 2; done
```

Rebuilding only `assistant` leaves the MCP sidecars untouched. Always run the test suite
first (`docker compose run --rm test`) — a broken image takes down live memo capture.

## Smoke test — webhook / chat

The app listens on port 8000 inside Docker only (not published to the host; prod traffic
arrives via the nginx proxy, which injects the Authorization header). The container has
no `curl` — use this in-container urllib pattern for any endpoint:

```bash
docker compose exec -T assistant python - <<'EOF'
import json, os, urllib.request
tok = os.environ.get("WEBHOOK_TOKEN", "")
hdrs = {"Content-Type": "application/json"}
if tok: hdrs["Authorization"] = f"Bearer {tok}"
req = urllib.request.Request("http://localhost:8000/webhook",
    data=json.dumps({"text": "test memo: pipeline verification, safe to file"}).encode(), headers=hdrs)
print(urllib.request.urlopen(req).read().decode())   # → {"status":"received",...}
EOF
```

Same pattern for `/chat` (`{"message": ..., "conversation_id": optional}`, allow ~120s —
a vault-retrieval question like "do I have notes about X?" is the best end-to-end probe)
and `/process` / `/daily-summary` (POST, empty body).

After a webhook test, confirm the pipeline: `docker compose logs assistant --since 2m`
shows `webhook tool call: write_to_vault ...`, and the memo lands in
`data/obsidian-vault/Josh-Personal/inbox.md`.

## Manual trigger — inbox processing

**Trigger via the live app's HTTP endpoint, not a standalone `python3 -c`.** The MCP
sessions (Todoist, Brave, obsidian-mcp) are opened once in the app's lifespan startup and
live in `app/ai/mcp.py`. A fresh `python3 -c` process has never called
`mcp.open_sessions()`, so no MCP tools are available — and the processor now refuses to
run at all without the obsidian-mcp tools (the inbox is preserved). Always go through
`/process`:

```bash
# Port 8000 is NOT published to the host, so POST from inside the container.
docker compose exec assistant python -c "
import os, urllib.request
tok = os.environ.get('WEBHOOK_TOKEN','')
req = urllib.request.Request('http://localhost:8000/process', method='POST',
                             headers={'Authorization': f'Bearer {tok}'})
print('HTTP', urllib.request.urlopen(req, timeout=10).status)  # → HTTP 200
"
```

`/process` runs `process_inbox()` as a background task inside the live app. The companion
endpoint `/daily-summary` triggers the summary the same way. Both require the
`Authorization: Bearer $WEBHOOK_TOKEN` header.

Watch logs (processing is async; it can take 30–90s over many model turns):

```bash
docker compose logs -f assistant
# Look for: "Starting inbox processing"
# Look for: "inbox-processor tool call: ..."
# Look for: "Inbox processing complete"
```

## Manual trigger — daily summary

POST to `/daily-summary` using the in-container urllib pattern above (same bearer auth).
Expected log output:

```
INFO app.processors.daily_summary: Generating daily summary for YYYY-MM-DD (weekday=True/False)
INFO app.processors.daily_summary: Created daily note YYYY-MM-DD.md
```

The generated note appears at `data/obsidian-vault/Josh-Personal/notes/daily/YYYY-MM-DD.md`.
Note: triggering this before the 07:00 scheduled run creates today's note early; the
scheduled run will then skip (it skips whenever `## Yesterday` already exists in the note).

## Diagnosing MCP sidecar failures

obsidian-mcp is memory-capped (`mem_limit: 256m`) and **restarts under the load of a
processing run** (big directory listings/searches). That kills the sessions the app
opened at startup. The app self-heals: every MCP call has a 90s read timeout, and a
failed call triggers one reconnect + retry. This failure mode silently broke every
inbox-processing run 2026-07-08 → 2026-07-19 (before the reconnect logic existed).

When tool calls hang, fail, or a processor run dies:

```bash
docker inspect assistant-obsidian-mcp-1 --format 'restarts={{.RestartCount}} started={{.State.StartedAt}}'
docker stats --no-stream assistant-obsidian-mcp-1 --format "{{.MemUsage}}"
docker compose logs assistant --since 30m | grep -E "reconnect|MCP call .* failed"
# Healthy recovery looks like: "MCP call read_note failed — reconnecting sessions and
# retrying once" followed by "MCP sessions reconnected (62 tools)".
```

A run killed mid-processing never loses memos: the staged `inbox.processing.md` is
folded into the next run.

## Probing obsidian-mcp from a Claude Code session

The sidecars publish loopback ports (`.mcp.json`), so host-side Claude Code sessions get
`mcp__obsidian-vault__*` tools talking to the **same live vault** — useful for probing
tool semantics or cleaning up without going through the app. Learned semantics:

- `write_note` has `mode: overwrite (default) | append | prepend`, plus an optional
  `frontmatter` object; inline frontmatter in `content` is preserved byte-verbatim.
  Files it creates are owned by uid 1000 (the vault owner) — this is why app writes
  prefer MCP over direct file I/O.
- `read_note` returns JSON `{"fm": {...}, "content": ...}` with frontmatter split out —
  it is NOT a verbatim read; don't use it where exact bytes matter.
- `delete_note` requires `confirmPath` identical to `path`; `move_note`/`move_file`
  require `confirmOldPath`/`confirmNewPath`.
- Probe pattern: write a scratch note under `archive/`, inspect the file on disk
  (`data/obsidian-vault/Josh-Personal/...`), then `delete_note` it.

## Run tests

```bash
docker compose run --rm test
```

Or a single file:

```bash
docker compose run --rm test python -m pytest tests/test_webhook.py -v
```

Note: the `test` service has `ANTHROPIC_API_KEY=""` intentionally — no real API calls in tests.

## Stop

```bash
docker compose down
```

## Key gotchas

- **Port access**: the assistant container has no `curl`. Use `wget` inside the container or expose port 8000 to the host.
- **Timezone**: if `TZ` is not set in `.env`, the container defaults to UTC and the scheduler fires at UTC times, not BST.
- **Vault path**: the vault is mounted at `/vault` inside the container from `./data/obsidian-vault/Josh-Personal` on the host.
- **Inbox skip**: the processor no-ops only when the inbox has no `## timestamp` entries **and** there is no recent context. It also consumes the daily notes (today + yesterday) and any vault note modified within `RECENT_CONTEXT_HOURS` (default 24h) as **read-only** input — items are filed from them but the source notes are never cleared/moved/deleted. So it can run a real pass even with an empty inbox.
- **Action preservation**: every mutating tool call is logged append-only to `archive/audit/YYYY-MM-DD.md`; `archive_inbox` snapshots raw `inbox.md` verbatim before clearing. To see what a run did, read the dated audit log.
- **Daily note skip**: the daily summary processor skips if `## Yesterday` already exists in today's note.
- **MCP tools**: all tools are discovered at runtime from the sidecar containers. If a sidecar is down at startup, the agent proceeds with whatever tools are available — except the inbox processor, which skips its run entirely without obsidian-mcp (inbox preserved).
- **File ownership**: the app container runs as root; vault writes go through obsidian-mcp (uid 1000) precisely so files stay sync-manageable. If root-owned files ever appear in the vault (`find data/obsidian-vault/Josh-Personal -user 0`), a direct-write fallback path missed its chown — fix the path, then chown the files via `docker compose exec assistant chown -R 1000:1000 <paths>`.
