"""Shared agentic loop used by the webhook agent, the inbox processor, and chat.

Callers supply the prompt, tool definitions, and an async `dispatch(name, input)`
that executes one tool call; the loop handles the model round-trips, audit
recording, error capture, and tool_result assembly.
"""

import logging
from typing import Any, Awaitable, Callable

from app.ai import llm
from app.handlers import audit

log = logging.getLogger(__name__)

Dispatch = Callable[[str, dict], Awaitable[str]]
# on_action(tool_name, tool_input, result, error) — error is None on success
OnAction = Callable[[str, dict, str, Exception | None], None]


async def run_agent_loop(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    dispatch: Dispatch,
    model: str,
    max_turns: int,
    max_tokens: int,
    audit_source: str,
    on_action: OnAction | None = None,
    effort: str | None = None,
) -> str:
    """Run the tool-use loop until the model stops calling tools or max_turns.

    Mutates `messages` in place (so callers can keep conversation history) and
    returns the text of the model's final assistant message.
    """
    if tools:
        # cache_control on the last tool caches the whole tool list prefix
        tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]

    extra: dict[str, Any] = {"output_config": {"effort": effort}} if effort else {}

    final_text = ""
    for turn in range(max_turns):
        response = await llm.get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            **extra,
        )
        log.info("%s turn %d: stop_reason=%s", audit_source, turn, response.stop_reason)

        # Thinking and response text share max_tokens. A truncated turn stops the
        # loop exactly like a completed one, so without this it looks like clean
        # success — mid-loop that means half-finished work reported as done.
        if response.stop_reason == "max_tokens":
            log.error(
                "%s turn %d hit max_tokens (%d) — output truncated, work may be incomplete",
                audit_source, turn, max_tokens,
            )
        messages.append({"role": "assistant", "content": response.content})

        text_parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        if text_parts:
            final_text = "\n".join(text_parts)

        if response.stop_reason != "tool_use":
            if turn == 0:
                log.info("%s took no tool action. Model said: %s", audit_source, final_text or "(no text)")
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            log.info("%s tool call: %s %s", audit_source, block.name, block.input)
            error: Exception | None = None
            try:
                result = await dispatch(block.name, block.input)
            except Exception as exc:
                log.exception("Tool %s failed", block.name)
                error = exc
                result = f"Error: {exc}"

            log.info("%s tool result: %.200s", audit_source, result)

            try:
                await audit.record(block.name, block.input, result, source=audit_source)
            except Exception:
                log.exception("Audit logging failed for %s", block.name)

            if on_action is not None:
                on_action(block.name, block.input, result, error)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    return final_text
