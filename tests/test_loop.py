import pytest

from app.ai import llm
from app.ai.loop import run_agent_loop
from app.config import settings
from tests.conftest import FakeClient, response, text_block, tool_block


@pytest.fixture
def vault_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    return tmp_path


def _install(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_tool_call_dispatch_and_result_roundtrip(vault_tmp, monkeypatch):
    client = _install(monkeypatch, [
        response("tool_use", tool_block("my_tool", {"x": 1})),
        response("end_turn", text_block("all done")),
    ])

    dispatched = []

    async def dispatch(name, inp):
        dispatched.append((name, inp))
        return "tool says hi"

    final = await run_agent_loop(
        system_prompt="sys", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "my_tool", "description": "", "input_schema": {"type": "object"}}],
        dispatch=dispatch, model="test-model", max_turns=5, max_tokens=100,
        audit_source="test",
    )

    assert dispatched == [("my_tool", {"x": 1})]
    assert final == "all done"
    # Second request carries the tool_result back to the model. History layout:
    # [user, assistant(tool_use), user(tool_results), assistant(final)]
    results = client.messages.calls[1]["messages"][2]["content"]
    assert results[0]["type"] == "tool_result"
    assert results[0]["content"] == "tool says hi"


@pytest.mark.asyncio
async def test_dispatch_error_becomes_tool_result(vault_tmp, monkeypatch):
    client = _install(monkeypatch, [
        response("tool_use", tool_block("boom", {})),
        response("end_turn", text_block("ok")),
    ])

    async def dispatch(name, inp):
        raise RuntimeError("kaput")

    seen = []
    await run_agent_loop(
        system_prompt="sys", messages=[{"role": "user", "content": "go"}],
        tools=[], dispatch=dispatch, model="test-model", max_turns=5, max_tokens=100,
        audit_source="test",
        on_action=lambda name, inp, result, error: seen.append((name, result, error)),
    )

    name, result, error = seen[0]
    assert name == "boom"
    assert result.startswith("Error:")
    assert isinstance(error, RuntimeError)
    # The loop keeps going — the error is fed back as a tool_result, not raised
    assert client.messages.calls[1]["messages"][2]["content"][0]["content"].startswith("Error:")


@pytest.mark.asyncio
async def test_max_turns_caps_runaway_loop(vault_tmp, monkeypatch):
    client = _install(monkeypatch, [
        response("tool_use", tool_block("t", {}, f"tu_{i}")) for i in range(10)
    ])

    async def dispatch(name, inp):
        return "again"

    await run_agent_loop(
        system_prompt="sys", messages=[{"role": "user", "content": "go"}],
        tools=[], dispatch=dispatch, model="test-model", max_turns=3, max_tokens=100,
        audit_source="test",
    )
    assert len(client.messages.calls) == 3


@pytest.mark.asyncio
async def test_refusal_with_empty_content_is_not_appended(vault_tmp, monkeypatch, caplog):
    """A pre-output refusal returns no content; an empty assistant turn stored in
    chat history would 400 every later request in that conversation."""
    _install(monkeypatch, [response("refusal")])
    messages = [{"role": "user", "content": "go"}]

    async def dispatch(name, inp):
        raise AssertionError("no tool should run")

    final = await run_agent_loop(
        system_prompt="sys", messages=messages, tools=[], dispatch=dispatch,
        model="test-model", max_turns=5, max_tokens=100, audit_source="test",
    )

    assert final == ""
    assert messages == [{"role": "user", "content": "go"}]
    assert any("refused" in r.message and r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_unknown_blocks_filtered_and_tools_still_dispatch(vault_tmp, monkeypatch):
    """A server-side fallback inserts a `fallback` marker block; it must not be
    echoed back or break tool dispatch."""
    from types import SimpleNamespace

    marker = SimpleNamespace(type="fallback")
    client = _install(monkeypatch, [
        response("tool_use", marker, tool_block("my_tool", {"x": 1})),
        response("end_turn", text_block("done")),
    ])
    dispatched = []

    async def dispatch(name, inp):
        dispatched.append(name)
        return "ok"

    await run_agent_loop(
        system_prompt="sys", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "my_tool", "description": "", "input_schema": {"type": "object"}}],
        dispatch=dispatch, model="test-model", max_turns=5, max_tokens=100,
        audit_source="test",
    )

    assert dispatched == ["my_tool"]
    echoed = client.messages.calls[1]["messages"][1]["content"]
    assert [b.type for b in echoed] == ["tool_use"]
    assert client.messages.calls[0]["extra_body"] == {"fallbacks": "default"}
