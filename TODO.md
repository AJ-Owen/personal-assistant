# TODO

## Follow-up work (2026-07-19)

- [x] **All vault writes via obsidian-mcp** — `vault.append_note`/`write_note` prefer MCP
  `write_note` (files created as the vault owner, uid 1000, so Obsidian Sync can manage
  them) with direct-write fallback + `chown` so capture never fails. Existing root-owned
  files in the vault were chowned to 1000:1000. Reads/mtime scans and the atomic staging
  rename stay direct-file (see CLAUDE.md for why).
- [x] **MCP resilience: call timeout + auto-reconnect** — root cause of 11 days of
  silent inbox-processing failures (2026-07-08 → 2026-07-19): obsidian-mcp hits its 256m
  memory cap under processing load and restarts, killing the startup-opened sessions;
  calls then hung forever. Now every call has a 90s read timeout and a failed call
  triggers one session reconnect + retry (`app/ai/mcp.py`, tests in `tests/test_mcp.py`).

---

Prioritized plan from the 2026-07-18 code review. **Implemented 2026-07-18** (all
phases except the two deliberately deferred items at the bottom); deployed and
verified end-to-end. Details of each change live in CLAUDE.md.

## Phase 1 — Data safety & bug fixes

- [x] **Fix inbox archive race** — `begin_inbox_processing` atomically renames
  `inbox.md` → `inbox.processing.md`; memos arriving mid-run land in a fresh inbox and
  are handled next cycle. `archive_inbox` consumes only the staged file and removes it
  only after a successful archive write; crash leftovers are folded into the next run.
  Covered by tests in `tests/test_vault.py`.
- [x] **Refuse silent note overwrites** — `vault.overwrite_guard` (wired into every
  agent dispatcher) refuses `write_note` in default overwrite mode against an existing
  note; appends/prepends/patches and new notes pass through. (Superseded the original
  `create_note` fix — that tool was removed entirely in 2c.)
- [x] **Shared machine-generated-path exclusion** — `vault.is_machine_path` used by both
  `list_recent_files` and the daily summary; fixes the audit-log-into-daily-summary leak
  (`archive/audit` was being fed to the summary every morning).
- [x] **Trivial cleanups** — removed the local `import re`; verified `WEBHOOK_TOKEN` is
  set in the prod `.env`. Also corrected CLAUDE.md's stale "auto-reload via mounted
  source" claim — source is baked into the image; rebuild to deploy.

## Phase 2 — Structural refactor

- [x] **2a. Extract MCP session + LLM client modules** — `app/ai/mcp.py` (sessions,
  tool discovery, `call_tool`) and `app/ai/llm.py` (client, user context, system-prompt
  assembly). No more cross-module `_private` reach-ins.
- [x] **2b. Shared agent loop** — `run_agent_loop` in `app/ai/loop.py`, used by the
  webhook agent, inbox processor, and chat. Handles model round-trips, audit recording,
  error capture, and tool_result assembly.
- [x] **2c. Unified vault tool surface** — processor keeps only `read_inbox` +
  `archive_inbox` locally; all search/read/create/append goes through obsidian-mcp
  (`search_notes`, `write_note` mode append, `patch_note`, `list_directory`). Duplicate
  local tools (`search_vault`, `create_note`, `append_to_note`, `list_vault`) removed.
  Processor skips its run (inbox preserved) if obsidian-mcp is down.

## Phase 3 — Hardening & quality

- [x] **Tests for the risky code** — `tests/test_loop.py` (dispatch round-trip, error
  path, max-turns cap), `tests/test_inbox_processor.py` (archive, safety-archive,
  MCP-down skip, no-op), `tests/test_daily_summary.py` (machine-file exclusion,
  already-generated skip), plus staging/guard tests in `tests/test_vault.py`.
  57 tests passing.
- [x] **Per-role models** — `AGENT_MODEL` (haiku), `PROCESSOR_MODEL` (sonnet),
  `SUMMARY_MODEL` (haiku), `CHAT_MODEL` (sonnet) in `config.py`.
- [x] **Persist `/history` across restarts** — JSONL at `HISTORY_PATH`
  (`/data/history.jsonl` in compose), rewritten on add and after each agent run.

## Phase 4 — Second-brain features

- [x] **Conversational endpoint** — `POST /chat` (`app/routers/chat.py`) + a Chat tab
  in the web UI. Multi-turn, full MCP toolset, retrieval-oriented prompt, bounded
  in-memory conversation store.
- [ ] **Evaluate semantic search — deliberately deferred.** First measure how far
  obsidian-mcp `search_notes` + whole-note reads get on the real vault (via the new
  /chat surface). Only build an embeddings index sidecar if retrieval quality is
  actually lacking.
- [ ] **google-calendar-mcp** — blocked on OAuth2 credentials (needs a Google Cloud
  project + consent screen + credentials mounted into the container). Wiring is now
  trivial: URL in `config.py`, sidecar in `docker-compose.yml`, URL appended in
  `mcp.open_sessions()`.
