"""Webhook capture agent: one incoming memo → tool actions (vault/Todoist/search)."""

import logging
from typing import Any

from app.ai import llm, mcp
from app.ai.loop import run_agent_loop
from app.config import settings
from app.handlers import notify, vault

log = logging.getLogger(__name__)

_BASE_INSTRUCTIONS = """\
## Instructions

Take the appropriate action(s) using your tools. Every message must result in at least one tool call — \
if no other tool applies, fall back to `write_to_vault` so the message lands in the user's knowledge base. \
The only things that should NOT be vaulted are content already captured by another tool (Todoist tasks, \
calendar events, etc.) — don't duplicate those into the vault.

**Vault vs. Todoist — these are not symmetric.** A note filed to the vault is reviewed and re-filed by the \
daily inbox processor, so a wrong guess costs nothing. A task added to Todoist is never reviewed by anything \
— a wrong guess sits in the user's task list forever. **When you are not confident the message is a task, \
vault it.**

Only use Todoist when the message describes something the user still has to *do*. In particular:
- **Past tense is never a task.** "Cycled to work today", "cycle commute, 35 mins door to shower", \
"finished the plaque" are records of something already done → `write_to_vault`.
- **A measurement, duration, or observation is never a task**, even with no verb — "35 mins home to work", \
"25° weather" → `write_to_vault`.
- **An idea or a possibility is not a task.** "sam bday present idea, new kindle case" is an idea → \
`write_to_vault`. "buy sam a kindle case" is a task.
- A bare noun phrase is a shopping item only when the context is clearly shopping ("nectarines to shopping \
list"); otherwise it is a note.

**write_to_vault** — the default catch-all. Use for notes, ideas, reference material, personal facts about \
the user ("I have brown hair", "my doctor is Dr. Smith"), preferences, observations, logs of things already \
done, search-result summaries, and anything else worth keeping. If nothing else fits, vault it. \
For search lookups, also include the `reply` field with a 1–2 sentence direct answer — it is shown in the \
web UI activity feed. `reply` does NOT reach the user's phone or ring: to actually tell them something, \
call `notify_user`.

**notify_user** — the only channel that reaches the user. Nothing you write as plain text is ever \
seen: the capture request has already returned by the time you run. Call it when:
- The user asked a **question** — send the answer. Always. An answer filed only to the vault has failed.
- You **could not do** what was asked, or did something the user might not expect.
- Something is **time-critical** and the user should know now rather than at the next daily review.

Do NOT notify for routine captures — a saved note or an added task needs no push. Keep messages to \
1–3 sentences and make them self-contained; the user sees only the notification.

**brave_web_search** — use when the user asks a factual or current-information question: \
weather, news, definitions, "who is", "what is", "look up", "search for", "find out". \
Before searching the web, consider whether the answer is already in the user's vault — if the question \
is about their own life, notes, or people, use `search_notes`/`read_note` first. \
After every search, call `write_to_vault` with label "lookup" to keep the finding, AND `notify_user` \
with the answer. Never end your turn after a search without doing both. \
If live data wasn't available (e.g. real-time flights, live prices), notify the user where to check \
(e.g. "Check heathrow.com/departures for live departures.").

**Todoist tools** — tasks, to-dos, reminders, shopping/grocery items, appointments.
- **Adding**: create a task in the correct project using the bare alphanumeric project ID from the user context \
(e.g. `6Xa1Bc2De3Fg4Hj5`). Never prepend a name or label to an ID. If the right project is not in your context, \
call find-projects first — do not guess.
- **Shopping list items**: strip the action verb and write only the item. \
"buy hummus" → "hummus", "get 6 eggs" → "6 eggs", "pick up sandwich meat" → "sandwich meat". \
For quantities, use the format "item (N)" — "get a 6 pack of beer" → "beer (6)".
- **Modifying, completing, deleting, or moving**: ALWAYS call a Todoist search or list tool first to find \
the task by name and get its ID. Never guess or fabricate a task ID. Then perform the operation using that ID.

**Vault tools (mcpvault)** — for working with *existing* notes: read_note, read_multiple_notes, \
search_notes, list_directory, write_note, patch_note, move_note, delete_note, get/update_frontmatter, \
manage_tags, get_vault_stats. Use these when the user asks to find, read, reorganise, retag, or edit \
notes already in the vault, or to make bulk changes across files. For brand-new capture, prefer \
write_to_vault (it appends to the inbox). To add to an existing note, use write_note with mode \
'append' or patch_note — never overwrite a note that already exists. Only delete or move notes when \
the user explicitly asks.

A single message may warrant multiple tools (e.g. "remind me to write a blog post about X" → vault note + Todoist task).

If a message has no actionable tool match, save it to the vault rather than ignoring it.

When creating a Todoist task with a due date, always include a specific time: use what the user stated, \
infer from context ("lunch with Alex" → 12:30 pm), or default to 9:00 am.

Always fix obvious speech-to-text errors before using any tool. Keep the user's intent intact.
"""

_VAULT_TOOL_NAME = "write_to_vault"
_NOTIFY_TOOL_NAME = "notify_user"

_NOTIFY_TOOL: dict[str, Any] = {
    "name": _NOTIFY_TOOL_NAME,
    "description": (
        "Send a push notification to the user's phone. This is the ONLY way to tell the user "
        "something — the ring and the capture API are one-way and never show your text. Use it to "
        "answer questions, report what you could not do, and flag anything needing attention."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The notification body. Self-contained: the user has no other context."},
            "title": {"type": "string", "description": "Short title line, e.g. 'Eclipse' or 'Could not add task'."},
            "tags": {"type": "string", "description": "Optional comma-separated ntfy emoji tags, e.g. 'mag' for a lookup, 'warning' for a problem."},
        },
        "required": ["message"],
    },
}

_VAULT_TOOL: dict[str, Any] = {
    "name": _VAULT_TOOL_NAME,
    "description": "Save a note, idea, piece of knowledge, or search result summary to the Obsidian vault inbox.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The note content to save"},
            "label": {"type": "string", "description": "Short category label, e.g. 'idea', 'reference', 'meeting', 'lookup'"},
            "reply": {"type": "string", "description": "1–2 sentence direct answer, shown in the web UI activity feed (not on the ring). Required for label 'lookup'; omit for plain notes."},
        },
        "required": ["content"],
    },
}

# Internal lookups — not shown in the activity feed.
_SILENT_TOOLS = {
    # Todoist
    "find-tasks", "find-tasks-by-date", "find-projects", "find-sections",
    "find-labels", "find-filters", "find-reminders", "find-activity",
    "find-comments", "find-completed-tasks", "find-project-collaborators",
    "search", "fetch", "fetch-object", "get-overview", "get-productivity-stats",
    "get-project-activity-stats", "get-project-health", "analyze-project-health",
    "get-workspace-insights", "list-workspaces", "user-info", "view-attachment",
    # obsidian-mcp read-only tools
    "read_note", "read_multiple_notes", "search_notes", "list_directory",
    "list_all_tags", "get_frontmatter", "get_notes_info", "get_vault_stats",
}


def _summarize_action(tool: str, inp: dict) -> str | None:
    """Return a human-readable one-liner for an action, or None to suppress it."""
    if tool in _SILENT_TOOLS:
        return None

    if tool == _VAULT_TOOL_NAME:
        label = inp.get("label", "")
        content = inp.get("content", "")
        preview = content[:60] + ("…" if len(content) > 60 else "")
        return f"Saved note{' · ' + label if label else ''}: {preview}"

    if tool == _NOTIFY_TOOL_NAME:
        msg = inp.get("message", "")
        return f"Notified: {msg[:60]}" + ("…" if len(msg) > 60 else "")

    if tool in ("brave_web_search", "brave_local_search"):
        return f"Searched: \"{inp.get('query', '')}\""

    if tool in ("write_note", "patch_note"):
        return f"Edited note: {inp.get('path', '')}"

    if tool in ("move_note", "move_file"):
        return f"Moved note: {inp.get('path', inp.get('source', ''))}"

    if tool == "delete_note":
        return f"Deleted note: {inp.get('path', '')}"

    if tool == "add-tasks":
        tasks = inp.get("tasks", [])
        content = tasks[0].get("content", "") if tasks else ""
        return f"Added task: {content}"

    if tool == "complete-tasks":
        return "Marked task complete"

    if tool == "uncomplete-tasks":
        return "Marked task incomplete"

    if tool == "update-tasks":
        tasks = inp.get("tasks", [])
        content = tasks[0].get("content", "") if tasks else ""
        return f"Updated task{': ' + content if content else ''}"

    if tool == "reschedule-tasks":
        return "Rescheduled task"

    if tool == "delete-object":
        obj_type = inp.get("type", "item")
        return f"Deleted {obj_type}"

    if tool == "add-reminders":
        return "Added reminder"

    if tool == "update-reminders":
        return "Updated reminder"

    if tool == "project-move":
        return "Moved to project"

    if tool == "add-projects":
        name = inp.get("name", "")
        return f"Created project{': ' + name if name else ''}"

    if tool == "add-sections":
        name = inp.get("name", "")
        return f"Created section{': ' + name if name else ''}"

    if tool == "add-comments":
        return "Added comment"

    # Generic fallback for anything else that isn't a silent lookup
    return tool.replace("-", " ").title()


async def run(text: str, entry: dict | None = None) -> None:
    tools: list[dict[str, Any]] = [_VAULT_TOOL]
    if notify.enabled():
        tools.append(_NOTIFY_TOOL)
    if mcp.connected():
        tools = [*tools, *mcp.tools()]
    else:
        log.warning("No MCP sessions available — running with vault tools only")

    tool_calls = 0

    async def dispatch(name: str, inp: dict) -> str:
        nonlocal tool_calls
        tool_calls += 1
        guard = vault.overwrite_guard(name, inp)
        if guard is not None:
            return guard
        if name == _NOTIFY_TOOL_NAME:
            return await notify.send(
                inp["message"], title=inp.get("title"), tags=inp.get("tags")
            )
        if name == _VAULT_TOOL_NAME:
            await vault.append_to_vault(inp["content"], label=inp.get("label"))
            if entry is not None and inp.get("reply"):
                entry["reply"] = inp["reply"]
            return "Saved to vault."
        if mcp.has_tool(name):
            return await mcp.call_tool(name, inp)
        return f"Unknown tool: {name}"

    def on_action(name: str, inp: dict, result: str, error: Exception | None) -> None:
        if entry is None:
            return
        if error is not None:
            entry["actions"].append({"text": f"Error in {name}: {error}", "error": True})
            return
        summary = _summarize_action(name, inp)
        if summary is not None:
            entry["actions"].append({"text": summary})

    try:
        await run_agent_loop(
            system_prompt=llm.build_system_prompt(_BASE_INSTRUCTIONS),
            messages=[{"role": "user", "content": text}],
            tools=tools,
            dispatch=dispatch,
            model=settings.agent_model,
            # Answering a question now needs search → read → notify (→ vault),
            # which does not fit the original 5.
            max_turns=8,
            # Opus 5 thinks by default and max_tokens caps thinking + text
            # together — 1024 truncated mid-answer.
            max_tokens=4096,
            audit_source="webhook",
            on_action=on_action,
            # Classify-and-route needs judgment, not deep reasoning. Medium keeps
            # thinking short, which both cuts latency and leaves max_tokens
            # headroom so a multi-tool turn can't truncate mid-work.
            effort="medium",
        )

        # The prompt requires at least one tool call, but nothing enforced it:
        # a turn that ended with plain text silently discarded the memo and still
        # reported "done". File it rather than lose it, and surface it in /history.
        if tool_calls == 0:
            log.warning("Agent took no tool action for %r — filing to vault", text)
            await vault.append_to_vault(text, label="unfiled")
            # "I didn't know what to do with this" is the single most useful thing
            # to be told — silence here is how the two July/August memos were lost.
            await notify.send(
                f"Captured but not filed anywhere specific:\n\n{text}",
                title="Needs a look",
                tags="warning",
            )
            if entry is not None:
                entry["actions"].append({
                    "text": f"No action chosen — filed to inbox unchanged: {text[:60]}",
                    "error": True,
                })
                entry["status"] = "unfiled"
        elif entry is not None:
            entry["status"] = "done"
    except Exception:
        if entry is not None:
            entry["status"] = "error"
        raise
