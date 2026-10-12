# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All dev tasks run inside Docker — do not use local pip/venv/uv installs.

```bash
# Start the stack. NOTE: app source is baked into the image (no source mount) —
# code changes require a rebuild: docker compose up -d --build assistant
docker compose up --build

# View logs from a specific service
docker compose logs -f assistant        # Follow main app logs
docker compose logs -f todoist-mcp      # Follow Todoist MCP logs
docker compose logs -f brave-search-mcp # Follow Brave Search logs

# Run all tests. NOTE: use the `test` service, not `assistant` — `tests/` is in
# .dockerignore, so it is not in the assistant image; the test service bind-mounts
# the source instead (and so picks up code changes without a rebuild).
docker compose run --rm test

# Run a single test file
docker compose run --rm test python3 -m pytest tests/test_webhook.py -v

# Open a shell in the container (useful for debugging)
docker compose run --rm assistant bash

# Test the webhook endpoint directly
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"text": "test note"}'
```

## Architecture

```
webhook POST /webhook  ──►  _extract_text()  ──►  BackgroundTasks
                                                         │
                                            vault.record_capture(text)   (verbatim, archive/capture)
                                                         │
                                               agent.run(text)   ── run_agent_loop ──►  Claude
                                                    │           │          │
                                             write_to_vault  web_search  todoist/obsidian MCP tools
                                             (vault handler) (Brave API) (MCP sidecars)

POST /chat  ──►  multi-turn conversation against the vault (same loop, full MCP toolset)
GET /  ──►  templates/index.html  (mobile web UI: Capture + Chat tabs)
GET /history  ──►  recent entries in JSON (persisted to HISTORY_PATH across restarts)
```

## Code Structure

```
app/
├── main.py              # FastAPI app initialization, lifespan (MCP session lifecycle)
├── config.py            # Environment variables & settings (incl. per-role models)
├── history.py           # Activity feed, optionally persisted as JSONL (HISTORY_PATH)
├── scheduler.py         # APScheduler cron jobs (inbox processing, daily summary)
├── routers/
│   ├── webhook.py       # POST /webhook endpoint, CANDIDATE_FIELDS extraction
│   ├── chat.py          # POST /chat — multi-turn chat against the vault
│   ├── process.py       # POST /process, POST /daily-summary manual triggers
│   └── ui.py            # GET /, GET /history for the web UI
├── ai/
│   ├── llm.py           # Shared AsyncAnthropic client + system-prompt assembly
│   ├── mcp.py           # MCP session lifecycle, tool discovery, call_tool dispatch
│   ├── loop.py          # run_agent_loop — shared tool-use loop (audit, errors, results)
│   └── agent.py         # Webhook capture agent: prompt, write_to_vault, action feed
├── processors/
│   ├── inbox.py         # Daily inbox filing (staged inbox + MCP vault tools)
│   └── daily_summary.py # Morning daily-note generation
└── handlers/
    ├── vault.py         # Inbox capture/staging, overwrite guard, vault scans
    ├── notify.py        # ntfy push — the only outbound channel to the user
    └── audit.py         # Append-only action log
```

**Agentic loop:** All three agent surfaces (webhook agent, inbox processor, chat) run through `run_agent_loop` in `app/ai/loop.py` — callers supply the prompt, tool list, and an async `dispatch(name, input)`; the loop handles model round-trips, audit recording, error capture, and tool_result assembly. It stops when `stop_reason != "tool_use"` or at the caller's `max_turns` (webhook: 8, chat: 10, processor: 15) — the webhook's 8 has to cover search → read → notify → file for a question, not just a single capture. It also logs at ERROR on `stop_reason == "max_tokens"`: a truncated turn ends the loop identically to a completed one, so without that a half-finished run looks like clean success. Callers may pass `effort` (mapped to `output_config.effort`); the webhook agent uses `medium`, since classify-and-route needs judgment rather than deep reasoning and shorter thinking leaves `max_tokens` headroom.

**MCP integration:** Owned by `app/ai/mcp.py`. Each MCP sidecar has its own URL in `config.py`. At FastAPI startup (`mcp.open_sessions`), the app iterates `[todoist_mcp_url, brave_search_mcp_url, obsidian_mcp_url]` and connects to each via `streamablehttp_client`. Tools are collected into a flat tool→session dispatch map keyed by tool name, exposed via `mcp.tools()`, `mcp.has_tool()`, and `mcp.call_tool()`. If a sidecar is unavailable, it is skipped — the app continues with whatever backends did connect (except the inbox processor, which skips its run entirely if obsidian-mcp is down, preserving the inbox). Every `call_tool` has a 90s read timeout, and a failed call triggers one automatic session reconnect + retry — this matters because obsidian-mcp is memory-capped and restarts under heavy load, killing the sessions opened at startup (this silently broke every inbox-processing run between 2026-07-08 and 2026-07-19). Each connection's async contexts (`streamablehttp_client` + `ClientSession` are anyio task groups) are owned by a dedicated `_Connection` task, and `close()` only signals that task — closing them from a different task corrupts the opener's cancel-scope stack; before this design, a reconnect cancelled the FastAPI lifespan task and silently shut down the APScheduler jobs (2026-07-19 → 2026-07-23, no inbox runs or daily summaries). Adding a new MCP requires a new URL setting in `config.py`, a sidecar in `docker-compose.yml`, and appending the URL to the list in `mcp.open_sessions()`.

**Payload extraction:** the router branches on `Content-Type`. The Pebble Index 01 posts `multipart/form-data` (fields: `transcription`, optional `audio` as `audio/mp4`, `recordedAt`, `client`); the web UI and other JSON clients post `application/json`. Both paths build a plain `dict` and hand it to `_extract_text()`, which tries `CANDIDATE_FIELDS` — an ordered tuple — in order. `transcription` (the Pebble's field) is tried first. JSON bodies are logged raw at INFO; multipart requests log only the field names (the raw body may contain binary audio).

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | Required for agent; omit to skip AI |
| `VAULT_PATH` | `/vault` | Absolute path to Obsidian vault root |
| `INBOX_FILENAME` | `inbox.md` | File within vault to append entries |
| `WEBHOOK_TOKEN` | `""` | Bearer token; empty = auth disabled |
| `VAULT_HOST_PATH` | `./vault-dev` | Docker host path for vault volume |
| `TODOIST_API_KEY` | `""` | Todoist API token; passed to the MCP sidecar |
| `TODOIST_MCP_URL` | `http://todoist-mcp:3000/mcp` | Streamable HTTP URL of the Todoist MCP server |
| `BRAVE_SEARCH_MCP_URL` | `http://brave-search-mcp:3001/mcp` | Streamable HTTP URL of the brave-search MCP sidecar |
| `BRAVE_API_KEY` | `""` | Brave Search API key; passed to the brave-search-mcp sidecar |
| `USER_CONTEXT_PATH` | `""` | Path to a markdown file with personal context injected into the system prompt |
| `OBSIDIAN_MCP_URL` | `http://obsidian-mcp:3002/mcp` | Streamable HTTP URL of the obsidian-mcp (MCPVault) sidecar — filesystem vault tools |
| `OBSIDIAN_REMOTE_PASSWORD` | `""` | Password for the obsidian-remote web UI (set before exposing port 8080) |
| `TZ` | `UTC` | Timezone passed to obsidian-remote |
| `AUDIT_LOG_PATH` | `archive/audit` | Vault-relative dir for the append-only action log (one dated file per day) |
| `CAPTURE_LOG_PATH` | `archive/capture` | Vault-relative dir for the verbatim raw-capture log written before the agent runs |
| `NTFY_URL` | `http://ntfy:80` | ntfy base URL, reached over docker_proxy_network (no egress needed) |
| `NTFY_TOPIC` | `""` | ntfy topic to publish to; **empty disables notification and hides the tool from the model** |
| `NTFY_TOKEN` | `""` | ntfy access token for the dedicated write-only `assistant` user |
| `AGENT_MODEL` | `claude-opus-5` | Model for the webhook capture agent |
| `PROCESSOR_MODEL` | `claude-sonnet-5` | Model for the once-daily inbox processor (judgment-heavy filing) |
| `SUMMARY_MODEL` | `claude-sonnet-5` | Model for the daily-note summary generator |
| `CHAT_MODEL` | `claude-sonnet-5` | Model for the /chat conversational endpoint |
| `HISTORY_PATH` | `""` | JSONL file persisting the /history feed across restarts (set to `/data/history.jsonl` in compose); empty = in-memory only |

## Key Design Decisions

- **Per-role models** — Opus (`AGENT_MODEL`) for the webhook capture agent, Sonnet (`PROCESSOR_MODEL`, `SUMMARY_MODEL`, `CHAT_MODEL`) for inbox filing, the morning summary, and chat. The capture agent was on Haiku on the premise that it "runs constantly" — measured volume is ~1.3 captures/day, and since `/webhook` returns before the agent runs, latency is not a constraint either. Its failure mode is also the expensive one: a memo misfiled to the vault is corrected by the daily processor, a memo wrongly turned into a Todoist task is not. The whole stack costs ~£1/month at this volume. **Note `max_tokens` must leave room for thinking** — Opus 5 thinks by default and `max_tokens` caps thinking + text together; the webhook agent's old 1024 truncated mid-answer (now 4096). System prompts are prompt-cached (`cache_control: ephemeral`) to cut repeated token cost.
- **Capture durability does not depend on the model** — `vault.record_capture` appends every incoming memo verbatim to a dated log under `CAPTURE_LOG_PATH` *before* `agent.run` is called, and if the agent's loop finishes having made **zero** tool calls, `agent.run` files the raw text to the inbox with label `unfiled` and marks the history entry `status: "unfiled"` (not `"done"`). Both guards exist because the prompt's "every message must result in at least one tool call" rule was unenforced: on 2026-07-26 and 2026-08-02 the agent ended two turns with plain text, and those memos reached neither the vault, Todoist, nor the audit log while `/history` showed them as successful. The capture log lives under `archive/` so `is_machine_path` keeps it out of inbox processing and the daily summary — it is a safety net, not a second inbox.
- **Person notes are the retrieval surface, so they are templated** — `notes/personal/people/Name.md`. The processor prompt specifies the vault's existing shape (`# Notable Info` / `# Knows` / `## See Also`, as in the human-authored person notes) rather than letting the model invent one, and requires two things that make later "what do I know about X?" questions answerable: an `aliases:` frontmatter list covering every name actually spoken ("Dad" plus the full name), and at least one `[[wikilink]]` under `## See Also`. Links are the mechanism — without them each note is an island and `search_notes` is the only route in. The chat prompt mirrors this: match on aliases via `list_directory` when a name search misses, then follow the note's links. The failure this fixes was real: a person note was created as a page of gift product research with no biographical facts and no links, so the "researched things are topic notes, linked from the person" rule is now stated explicitly.
- **Capture-time Todoist adds are audited daily** — the capture agent decides task-vs-note in the moment with no vault context, and nothing else ever reviews a Todoist task. `_recent_webhook_task_adds` scrapes the last 48h of audit logs for `webhook` + `add-tasks` lines and feeds them to the inbox processor, which files any that are really logs/observations/ideas into the vault and clears them via `find-tasks` + `complete-tasks`. The capture prompt biases the same way up front: vault when uncertain, and never treat past-tense reports or bare measurements as tasks.
- **`anthropic_api_key: str = ""`** — empty default so tests import cleanly without a real key. The Anthropic client treats an empty string as missing and raises on actual API calls.
- **Pebble Index 01** is just another webhook client — same `/webhook` endpoint. It POSTs `multipart/form-data` rather than JSON (see help.repebble.com "Index Advanced Features (MCP, Webhook)"), so the router content-type-sniffs and parses accordingly before running the same `CANDIDATE_FIELDS` extraction. The ring's audio field (`audio/mp4`, sent only if the "Send" setting includes recordings) is deliberately not persisted — text-only. Only double-click-hold is routed here; single-click-hold stays on Pebble's native agent (timers etc.), which works well and should not be taken over.
- **The webhook is one-way; ntfy is the reply path.** `/webhook` returns `{"status": "received"}` *before* the background agent runs, so nothing the agent produces can be in the response body (and Pebble's docs don't say the Index renders the body anyway). `reply` on `write_to_vault` is a web-UI activity-feed field only — it previously told the model the text "displays on a smartwatch", so the model wrote answers that reached nobody. The real channel is the local `notify_user` tool (`app/handlers/notify.py`), which pushes to ntfy and lands on the phone. That completes the ask-and-answer loop from the ring — question → vault search → push — **without** the Index "MCP Sandbox" build, which is therefore not needed for this use case (it would only add *spoken, on-ring* replies).
- **ntfy is a local tool, not an MCP sidecar** — it is one HTTP POST, so it lives in `app/handlers/notify.py` with a tool dict + `dispatch` branch in `agent.py`, not a container. It reaches `http://ntfy:80` over the shared `docker_proxy_network` (the ntfy container is on it), so the app needs no internet egress. Auth is a dedicated write-only ntfy user (`assistant`) scoped to the single topic `assistant` — deliberately **not** the `monitor` credential that fail2ban and Beszel publish under. `notify.send` never raises; a failed push must not fail the capture it was reporting on. `NTFY_TOPIC` empty disables the feature and the tool is then not offered to the model at all. It is intentionally *not* in `_SILENT_TOOLS`, so every push is recorded in the audit log — a record of what the assistant told you. **Notify sparingly**: questions, failures, and the `unfiled` fallback only. A push per shopping item trains you to ignore them.
- **Todoist via MCP sidecar** — `@doist/todoist-ai` runs as a Docker service. The agent discovers its tools at runtime via `session.list_tools()`, so new Todoist tool additions are picked up automatically without code changes.
- **Vault writes go through obsidian-mcp, with direct-write fallback** — the app container runs as root, but obsidian-mcp runs as the vault owner (uid 1000), so files created through it sync cleanly with Obsidian Sync. All app-originated writes (inbox capture, inbox archive, audit log, daily notes) call `vault.append_note`/`vault.write_note`, which prefer MCP `write_note` and fall back to direct file I/O so capture never fails when the sidecar is down. Direct fallback writes (and the inbox staging file) are `chown`ed to the vault owner afterwards (`_match_vault_owner`). Reads and mtime scans stay direct-file: MCP `read_note` returns JSON with frontmatter split out, which would break verbatim snapshots; the atomic inbox-staging rename also stays direct (rename preserves ownership).
- **Unified vault tool surface** — the inbox processor's only local tools are `read_inbox` and `archive_inbox` (its lifecycle); all search/read/create/append goes through the obsidian-mcp tools (`search_notes`, `write_note` with `mode: append`, `patch_note`, ...), so the model sees exactly one tool per capability. The old duplicate local tools (`search_vault`, `create_note`, `append_to_note`, `list_vault`) were removed.
- **Overwrite guard** — `vault.overwrite_guard` (wired into every dispatcher) refuses `write_note` calls in default overwrite mode against an existing note; appends/prepends/patches and new-note creation pass through. Prevents the model from silently destroying a note's content.
- **`brave_web_search` via MCP sidecar** — `brave-search-mcp` wraps `@modelcontextprotocol/server-brave-search` (stdio) with `supergateway` as a streamable HTTP server on port 3001. `BRAVE_API_KEY` is passed to the sidecar container; the app itself never touches the key.
- **Vault access via filesystem MCP (`obsidian-mcp`)** — `obsidian-mcp` wraps `@bitbonsai/mcpvault` (stdio) with `supergateway` as streamable HTTP on port 3002. It reads/writes the bind-mounted vault **directly on disk** (no Obsidian plugin, no REST, no API key) and exposes search/read/patch/move/delete/frontmatter/tag tools. It runs as `1000:1000` so files match what Obsidian expects. `obsidian-remote` runs headless Obsidian **purely as the Obsidian Sync bridge** — it watches the same directory and propagates the MCP's file changes to/from the user's devices. The two are decoupled: the MCP needs no running Obsidian, and Sync needs no REST server. (The old Local REST API plugin path was removed.) The port is published on `127.0.0.1:3002` so host-side Claude Code sessions use the same server via `.mcp.json`.
- **Action preservation** — every mutating tool call (note create/append/move/delete, Todoist adds, archive ops) is recorded to an append-only dated log under `AUDIT_LOG_PATH` (`app/handlers/audit.py`), wired into the shared agent loop (webhook, processor, and chat). Read-only lookups are skipped. Both logs live in the vault and sync to devices.
- **Staged inbox processing (race-safe)** — the processor never works on the live inbox. `begin_inbox_processing` atomically renames `inbox.md` → `inbox.processing.md`; memos arriving mid-run land in a fresh `inbox.md` and are handled next cycle. `archive_inbox` snapshots the staged file **verbatim** into the dated archive (independent of the model's `processed_log`) before removing it, and only removes it if the archive write succeeded. Leftover staged/staging files from a crashed run are folded back in on the next run — captured memos can never be lost.
- **User context** — `USER_CONTEXT_PATH` points to a markdown file (see `user_context.md.example`). Contents are prepended to the system prompt on every agent call. Re-read from disk each call so edits take effect without restart. Prompt caching still hits as long as content is unchanged between back-to-back calls.

## Web UI & API

- **GET /** — Serves `templates/index.html`, a mobile web UI with two tabs: Capture (submit memos + activity feed) and Chat (multi-turn conversation)
- **POST /webhook** — Accepts JSON with any of `CANDIDATE_FIELDS` (text, transcript, body, content, message, note, speech). Immediately writes raw entry to inbox, runs agent in background.
- **POST /chat** — `{"message": ..., "conversation_id": optional}` → `{"conversation_id", "reply", "actions"}`. Multi-turn chat with the full MCP toolset; conversations held in memory (bounded, per conversation_id). Same bearer auth as /webhook.
- **GET /history** — Returns recent entries in JSON format, used by the web UI to show activity feed
- **localhost:8000** — App port inside the Docker network (not published to the host; reached via the nginx proxy or `docker compose exec`)

## Networking

The app uses two docker networks:
- **docker_proxy_network** (frontend) — shared with the apps monorepo; allows nginx reverse proxy to reach the assistant service
- **ai_assistant_network** (ai-network, internal) — isolated network for MCPs and obsidian-remote; internet-facing requests come from MCP sidecars, not the app container

This setup allows the main assistant service to stay isolated while giving MCPs outbound internet access for web search and Todoist API calls.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| MCP service not found (connection refused) | Check `docker compose ps` — all MCPs should be running. Review `docker compose logs <service>` for startup errors. |
| MCP tool calls hang or fail mid-run | obsidian-mcp likely hit its 256m memory cap and restarted (check `docker inspect assistant-obsidian-mcp-1 --format '{{.RestartCount}}'`). The app reconnects and retries automatically; look for "MCP sessions reconnected" in the logs. |
| Vault writes/search fail | Check `VAULT_PATH` and that `obsidian-mcp` is up (`docker compose logs obsidian-mcp`). The sidecar runs as `1000:1000`; if writes are permission-denied, confirm the vault dir is owned by uid 1000. Device sync is separate — handled by `obsidian-remote` (Obsidian Sync). |
| Agent doesn't respond | Check `ANTHROPIC_API_KEY` is set in `.env`. If empty, agent skips and writes raw entry only. Review logs for Anthropic API errors. |
| Tests fail to import | Dockerfile test target sets `ANTHROPIC_API_KEY=""` intentionally (empty key in tests is safe). If tests fail for other reasons, check `docker compose run --rm test` output. |

## Development Workflow

**Adding a new MCP sidecar:**
1. Add Dockerfile in `build/Dockerfile.<name>`
2. Add service in `docker-compose.yml` with environment variables
3. Add URL setting in `app/config.py`
4. Append URL to the list in `app/ai/mcp.py`'s `open_sessions()` function
5. Rebuild: `docker compose up -d --build assistant`
6. Verify tools are discovered: check logs for `MCP connected` entries

**Modifying the agent behavior:**
- Webhook agent prompt: `app/ai/agent.py`; inbox processor prompt: `app/processors/inbox.py`; chat prompt: `app/routers/chat.py`
- Shared tool-calling loop: `app/ai/loop.py` (`run_agent_loop`)
- Tool implementations: `app/handlers/vault.py` (local) and the MCP sidecars via `app/ai/mcp.py`

**Testing webhook payloads:**
The Pebble Index 01's format is confirmed (see above) and handled. If a firmware update changes fields, check `docker compose logs -f assistant` — multipart requests log field names only (not the body, which may contain binary audio) — and extend `CANDIDATE_FIELDS` in `webhook.py` as needed.
