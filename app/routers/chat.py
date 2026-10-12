"""Multi-turn chat against the vault — the retrieval half of the second brain.

Conversations are held in memory per conversation_id (bounded), and each turn
runs the shared agent loop with the full MCP toolset.
"""

import logging
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.ai import agent, llm, mcp
from app.ai.loop import run_agent_loop
from app.config import settings
from app.handlers import vault
from app.routers.webhook import _check_auth

log = logging.getLogger(__name__)
router = APIRouter()

_CHAT_INSTRUCTIONS = """\
## Chat assistant

You are the user's personal assistant in a conversational session, with access to their \
Obsidian vault (search_notes, read_note, read_multiple_notes, list_directory, ...), Todoist, \
and web search.

- **Answer questions from the vault first.** Search and read the relevant notes before \
answering; mention which notes informed the answer (e.g. "from notes/personal/wiki/garden.md").
- **Questions about a person**: people live in `notes/personal/people/`. The user will use whatever name
  they normally say ("my dad", "Sam", "John") — notes carry an `aliases:` frontmatter list for exactly
  this, so if a direct search misses, `list_directory` on the people folder and match against aliases
  before concluding you have nothing. Then **follow the note's `[[wikilinks]]`** and `read_note` the ones
  that look relevant: the answer is often in a linked topic or another person's note, not in theirs. Also
  search more widely — someone can be mentioned in daily notes or topic notes without having their own note.
- Use web search only for current/external information the vault won't have.
- **Take actions (create/edit notes, add tasks) only when the user asks for them.** \
To add to an existing note use write_note with mode 'append' or patch_note — never overwrite \
an existing note. Use write_to_vault for quick new captures.
- If a search comes up empty, say so — don't invent vault content.
- Keep replies conversational and concise; this is a chat, not a report.
"""

_MAX_CONVERSATIONS = 20
_MAX_MESSAGES = 40

_conversations: "OrderedDict[str, list]" = OrderedDict()


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


def _trim(messages: list) -> list:
    """Bound history without splitting a tool_use/tool_result pair: drop from the
    front until the window starts at a plain user message."""
    if len(messages) <= _MAX_MESSAGES:
        return messages
    trimmed = messages[-_MAX_MESSAGES:]
    while trimmed and not (
        isinstance(trimmed[0], dict)
        and trimmed[0].get("role") == "user"
        and isinstance(trimmed[0].get("content"), str)
    ):
        trimmed.pop(0)
    return trimmed or messages[-2:]


@router.post("/chat", dependencies=[Depends(_check_auth)])
async def chat(body: ChatRequest):
    conv_id = body.conversation_id or uuid.uuid4().hex[:12]
    messages = _conversations.pop(conv_id, [])
    messages.append({"role": "user", "content": body.message})

    tools: list[dict[str, Any]] = [agent._VAULT_TOOL, *mcp.tools()]

    actions: list[dict] = []

    async def dispatch(name: str, inp: dict) -> str:
        guard = vault.overwrite_guard(name, inp)
        if guard is not None:
            return guard
        if name == agent._VAULT_TOOL_NAME:
            await vault.append_to_vault(inp["content"], label=inp.get("label"))
            return "Saved to vault."
        if mcp.has_tool(name):
            return await mcp.call_tool(name, inp)
        return f"Unknown tool: {name}"

    def on_action(name: str, inp: dict, result: str, error: Exception | None) -> None:
        if error is not None:
            actions.append({"text": f"Error in {name}: {error}", "error": True})
            return
        summary = agent._summarize_action(name, inp)
        if summary is not None:
            actions.append({"text": summary})

    reply = await run_agent_loop(
        system_prompt=llm.build_system_prompt(_CHAT_INSTRUCTIONS),
        messages=messages,
        tools=tools,
        dispatch=dispatch,
        model=settings.chat_model,
        max_turns=10,
        max_tokens=4096,
        audit_source="chat",
        on_action=on_action,
    )

    _conversations[conv_id] = _trim(messages)
    while len(_conversations) > _MAX_CONVERSATIONS:
        _conversations.popitem(last=False)

    return {"conversation_id": conv_id, "reply": reply, "actions": actions}
