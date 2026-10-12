---
description: Write or extend tests for the assistant's agent surfaces (loop, processors, vault) using the shared fakes — no real API or MCP calls
---

# Agent-tests skill — testing the assistant without real backends

Use this when adding or modifying tests for `run_agent_loop`, the inbox processor, the
daily summary, chat, or vault handlers. All tests run in Docker
(`docker compose run --rm test`); the test service sets `ANTHROPIC_API_KEY=""` so a
stray real call fails loudly.

## The shared fakes (`tests/conftest.py`)

```python
from tests.conftest import FakeClient, response, text_block, tool_block

client = FakeClient([
    response("tool_use", tool_block("archive_inbox", {"processed_log": "# log"})),
    response("end_turn", text_block("done")),
])
monkeypatch.setattr(llm, "get_client", lambda: client)
```

- `FakeClient(responses)` pops one scripted response per `messages.create` call and
  records every request in `client.messages.calls`. An empty list doubles as an
  assertion that no model call happens (a call would raise IndexError).
- Blocks are `SimpleNamespace`s: `text_block(t)`, `tool_block(name, input, block_id)`.

## Standard monkeypatch points

| To fake | Do |
|---|---|
| The model | `monkeypatch.setattr(llm, "get_client", lambda: FakeClient([...]))` |
| Vault location | `monkeypatch.setattr(settings, "vault_path", str(tmp_path))` — **always**; the loop's audit recorder and vault handlers write files |
| MCP available | `monkeypatch.setattr(mcp, "has_tool", lambda name: name == "search_notes")` + `monkeypatch.setattr(mcp, "tools", lambda: [])` |
| MCP calls | `monkeypatch.setattr(mcp, "call_tool", async_fake)` — or set `mcp._tool_to_session` to fake sessions when testing `mcp.call_tool` itself (see `tests/test_mcp.py`) |
| MCP absent | Nothing — tests run disconnected by default, exercising every direct-write fallback path |

## Gotchas that have burned real debugging time

1. **`run_agent_loop` mutates the `messages` list in place.** Every entry in
   `client.messages.calls` holds a reference to the *same* list, so after the run,
   `calls[0]["messages"][-1]` is the final assistant turn — not what was last when that
   call happened. Index positionally instead: history layout is
   `[user, assistant(tool_use), user(tool_results), assistant(final), ...]`, so the
   first turn's tool_results live at `calls[1]["messages"][2]`.
2. **Container localtime ≠ UTC.** `.env` sets `TZ=Europe/London`, and
   `monkeypatch.delenv("TZ")` does not reset libc's already-initialised timezone — a
   naive `datetime.now()` in the test stays on London time while code under test that
   read `os.getenv("TZ", "UTC")` after the delenv runs on UTC. Around midnight these
   disagree on the date. Always compute expected dates with
   `datetime.now(timezone.utc)` in tests that unset TZ.
3. **Data-safety invariants deserve tests, not comments.** When touching the inbox
   lifecycle, keep/extend the tests proving: a memo appended after
   `begin_inbox_processing` survives `archive_inbox`; crash leftovers
   (`inbox.processing.md`, `.staging`) are folded into the next run; the verbatim
   snapshot appears in the archive even when the model's log is wrong or missing.
4. **Fallback symmetry.** Any new MCP-preferred write needs two tests: MCP path used
   when available (assert via fake `call_tool`, and that no direct file appeared) and
   direct fallback when `call_tool` raises (assert the file exists — capture must never
   fail). See `test_append_prefers_mcp_write` / `..._falls_back_...` in
   `tests/test_vault.py`.

## Running

```bash
docker compose run --rm test                                        # full suite
docker compose run --rm test python -m pytest tests/test_loop.py -v # one file
# Debugging a silent skip/early return: add -rA --log-level=DEBUG and read captured logs
```
