# Personal Assistant

A self-hosted AI assistant that captures voice and text notes, files them into an Obsidian vault, adds tasks to Todoist, and processes your inbox daily.

## How it works

```
Pebble Index 01 ──►  POST /webhook  ──►  archive/capture/  (raw, immediate)
Web UI      ──►  POST /webhook  ──►  archive/capture/  ──►  Claude agent
                                                      │
                                    ┌───────────┬──┼──────────┬───────────┐
                               vault write   Todoist  web search   ntfy push
```

Every incoming note is appended verbatim to a dated raw-capture log (`archive/capture/`) the moment it arrives, before any model sees it — so a memo can never be lost, whatever the agent decides. Claude then runs in the background and files it: a structured note in the vault, a Todoist task, a web search, or all three. If it somehow picks nothing, the raw text is filed to `inbox.md` as `unfiled` and flagged in the activity feed.

At 06:00 (local `TZ`) each day, a scheduled job (Claude Sonnet) stages the inbox, classifies and files each entry into its correct permanent home in the vault, and archives the staged entries verbatim. Memos arriving mid-run are untouched and handled the next cycle.

There is also a **Chat** tab in the web UI (backed by `POST /chat`) for asking questions of the vault in a multi-turn conversation — the retrieval half of the second brain.

## Talking to the assistant

Speak or type naturally. The assistant infers intent from context — you don't need keywords or commands.

| What you say | What happens |
|---|---|
| `buy oat milk` | Added to Todoist (shopping list, no verb) |
| `call the dentist tomorrow morning` | Todoist task, due tomorrow 9am |
| `idea: build a hot composting bin` | Vault note in `notes/personal/wiki/` |
| `Sam recommended Preply for Spanish lessons` | Creates `notes/personal/wiki/preply.md` with referral link; credits `[[Sam]]` as source |
| `what's the best way to clean a cast iron pan?` | Web search, answer pushed to your phone and saved to the vault |
| `Dad's birthday is March 12th` | Appended to `notes/personal/people/Dad.md` (aliases resolve "Dad" → his full name) |
| `what do I know about Sam?` | Searches the vault, pushes the answer to your phone |
| `project: redesign the garden` | Vault note + optional Todoist project |
| `cycled to work, 35 mins door to shower` | Vault note — a past-tense report is a record, not a task |

**Tips:**
- The assistant leans toward the vault when it isn't sure. A vault note gets re-filed correctly by the 6am run; a wrong Todoist task would just sit in your list, so it needs to be confident before making one. The 6am run also re-checks the previous day's capture-time tasks and clears any that were really notes.
- **Ask it questions.** "What do I know about Carole?" searches your vault and pushes the answer to your phone via ntfy. The ring itself is one-way — the reply arrives as a notification, not on your finger.
- You also get a push when the assistant *couldn't* work out what to do with something, so nothing goes quietly missing.
- Shopping items are automatically stripped of the verb ("buy oat milk" → "oat milk" in Todoist)
- For tasks with a deadline, say it naturally: "by Friday", "end of month", "tomorrow at 3pm"
- You can mention a person by name and the assistant will file info under their note, creating one if it doesn't exist
- The daily inbox run at 6am handles anything captured overnight — no need to be precise in the moment

## Quick start

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, TODOIST_API_KEY, BRAVE_API_KEY
docker compose up --build
```

Open `http://localhost:8000` for the web UI.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Mobile web UI (Capture + Chat tabs) |
| `POST /webhook` | Capture a note (see below) |
| `POST /chat` | Multi-turn chat against the vault |
| `GET /history` | Recent entries as JSON |
| `POST /process` | Manually trigger inbox processing |
| `POST /daily-summary` | Manually trigger today's daily note generation |

### Webhook

`POST /webhook` accepts JSON with any of these fields (tried in order):

```
text · transcript · body · content · message · note · speech
```

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"text": "buy oat milk"}'
```

If `WEBHOOK_TOKEN` is set, include `Authorization: Bearer <token>`.

The Pebble Index 01 posts `multipart/form-data` instead of JSON, with the transcript in a `transcription` field (see the ["Index Advanced Features (MCP, Webhook)"](https://help.repebble.com/en/articles/15724406-index-advan) help article). The webhook route handles both formats — set the ring's webhook URL to `https://<your-host>/webhook`, "Send" to transcription (or both, if you also want raw audio uploaded — note audio isn't persisted yet), and add an `Authorization: Bearer <WEBHOOK_TOKEN>` header if auth is enabled.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | Required for AI; omit to write raw entries only |
| `VAULT_PATH` | `/vault` | Absolute path to vault root inside container |
| `INBOX_FILENAME` | `inbox.md` | File within vault to append captured entries |
| `WEBHOOK_TOKEN` | `""` | Bearer token for auth; empty = disabled |
| `TODOIST_API_KEY` | `""` | Todoist API token; passed to the MCP sidecar |
| `BRAVE_API_KEY` | `""` | Brave Search API key; passed to the search sidecar |
| `USER_CONTEXT_PATH` | `""` | Path to a markdown file with personal context for the prompt |
| `OBSIDIAN_MCP_URL` | `http://obsidian-mcp:3002/mcp` | Vault filesystem MCP sidecar (replaced the old Local REST API path) |
| `OBSIDIAN_REMOTE_PASSWORD` | `""` | Password for the obsidian-remote web UI |
| `CAPTURE_LOG_PATH` | `archive/capture` | Vault dir for the verbatim raw-capture log |
| `NTFY_URL` | `http://ntfy:80` | ntfy base URL for push notifications |
| `NTFY_TOPIC` | `""` | ntfy topic; empty disables notifications entirely |
| `NTFY_TOKEN` | `""` | ntfy access token (write-only user) |
| `AGENT_MODEL` | `claude-opus-5` | Model for the webhook capture agent |
| `PROCESSOR_MODEL` / `SUMMARY_MODEL` / `CHAT_MODEL` | `claude-sonnet-5` | Models for inbox filing, daily summary, and chat |
| `PROCESS_HOUR` | `6` | Hour (local TZ) to run the daily inbox processing |
| `DAILY_SUMMARY_HOUR` | `7` | Hour (local TZ) to generate the daily note summary |
| `TZ` | `UTC` | Timezone for scheduling |

## Services

| Service | Purpose |
|---|---|
| `assistant` | Main FastAPI app |
| `todoist-mcp` | Todoist MCP sidecar (task management) |
| `brave-search-mcp` | Brave Search MCP sidecar (web search) |
| `obsidian-remote` | Obsidian web UI (browse vault in browser) |

## Development

All dev tasks run inside Docker.

```bash
# Start the stack with live reload
docker compose up --build

# Tail logs
docker compose logs -f assistant

# Run tests (the `test` service — `tests/` is dockerignored, so the
# `assistant` image does not contain them and would run zero tests)
docker compose run --rm test

# Run a single test file
docker compose run --rm test python3 -m pytest tests/test_webhook.py -v

# Open a shell
docker compose run --rm assistant bash

# Trigger inbox processing manually
curl -X POST http://localhost:8000/process
```
