"""Tests for Anthropic prompt caching annotations."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from elasticity.backends.base import ToolSpec


# ---------------------------------------------------------------------------
# Helpers to instantiate AnthropicBackend without a real API key or SDK
# ---------------------------------------------------------------------------


def make_backend():
    """Return an AnthropicBackend instance with all external deps patched out."""
    mock_client = MagicMock()
    mock_anthropic_module = MagicMock()
    mock_anthropic_module.AsyncAnthropic.return_value = mock_client

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("elasticity.backends.anthropic.AsyncAnthropic", mock_anthropic_module.AsyncAnthropic):
            from elasticity.backends.anthropic import AnthropicBackend
            backend = AnthropicBackend()
            backend.client = mock_client
            return backend


# ---------------------------------------------------------------------------
# _build_system_param
# ---------------------------------------------------------------------------


def test_build_system_param_empty_string():
    backend = make_backend()
    result = backend._build_system_param("")
    assert result == ""


def test_build_system_param_none_like_falsy():
    backend = make_backend()
    # Any falsy value should return ""
    assert backend._build_system_param("") == ""


def test_build_system_param_with_content():
    backend = make_backend()
    result = backend._build_system_param("You are a helpful assistant.")
    assert isinstance(result, list)
    assert len(result) == 1
    block = result[0]
    assert block["type"] == "text"
    assert block["text"] == "You are a helpful assistant."
    assert block["cache_control"] == {"type": "ephemeral"}


def test_build_system_param_preserves_whitespace():
    backend = make_backend()
    prompt = "Line one.\n\nLine two."
    result = backend._build_system_param(prompt)
    assert result[0]["text"] == prompt


# ---------------------------------------------------------------------------
# _build_anthropic_tools
# ---------------------------------------------------------------------------


def _make_tools(*names):
    return [
        ToolSpec(name=n, description=f"Tool {n}", parameters={"type": "object", "properties": {}})
        for n in names
    ]


def test_build_anthropic_tools_none():
    backend = make_backend()
    assert backend._build_anthropic_tools(None) is None


def test_build_anthropic_tools_empty_list():
    backend = make_backend()
    assert backend._build_anthropic_tools([]) is None


def test_build_anthropic_tools_single_tool_gets_cache_control():
    backend = make_backend()
    result = backend._build_anthropic_tools(_make_tools("search"))
    assert len(result) == 1
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_build_anthropic_tools_cache_control_only_on_last():
    backend = make_backend()
    result = backend._build_anthropic_tools(_make_tools("read", "write", "search"))
    assert len(result) == 3
    assert "cache_control" not in result[0]
    assert "cache_control" not in result[1]
    assert result[2]["cache_control"] == {"type": "ephemeral"}


def test_build_anthropic_tools_preserves_schema():
    backend = make_backend()
    tools = [ToolSpec(name="foo", description="Foo tool", parameters={"type": "object", "properties": {"x": {"type": "string"}}})]
    result = backend._build_anthropic_tools(tools)
    assert result[0]["name"] == "foo"
    assert result[0]["description"] == "Foo tool"
    assert result[0]["input_schema"] == {"type": "object", "properties": {"x": {"type": "string"}}}


# ---------------------------------------------------------------------------
# _annotate_message_cache_breakpoint
# ---------------------------------------------------------------------------


def test_annotate_single_message_unchanged():
    backend = make_backend()
    messages = [{"role": "user", "content": "hello"}]
    result = backend._annotate_message_cache_breakpoint(messages)
    assert result == messages


def test_annotate_empty_messages_unchanged():
    backend = make_backend()
    assert backend._annotate_message_cache_breakpoint([]) == []


def test_annotate_string_content_converted_to_block():
    backend = make_backend()
    messages = [
        {"role": "user", "content": "first message"},
        {"role": "user", "content": "second message"},
    ]
    result = backend._annotate_message_cache_breakpoint(messages)
    # Second-to-last (index -2) should be annotated
    annotated = result[-2]
    assert isinstance(annotated["content"], list)
    assert annotated["content"][0]["type"] == "text"
    assert annotated["content"][0]["text"] == "first message"
    assert annotated["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Last message should be untouched
    assert result[-1]["content"] == "second message"


def test_annotate_list_content_adds_cache_to_last_block():
    backend = make_backend()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "abc", "content": "result"},
            ],
        },
        {"role": "user", "content": "new input"},
    ]
    result = backend._annotate_message_cache_breakpoint(messages)
    annotated_block = result[-2]["content"][-1]
    assert annotated_block["cache_control"] == {"type": "ephemeral"}
    assert annotated_block["tool_use_id"] == "abc"


def test_annotate_does_not_mutate_original():
    backend = make_backend()
    original = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    backend._annotate_message_cache_breakpoint(original)
    # Original should be unchanged
    assert original[0]["content"] == "first"
    assert isinstance(original[0]["content"], str)


def test_annotate_three_messages_targets_second_to_last():
    backend = make_backend()
    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": [{"type": "text", "text": "response"}]},
        {"role": "user", "content": "msg3"},
    ]
    result = backend._annotate_message_cache_breakpoint(messages)
    # index -2 is the assistant message
    assert result[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # index -3 (msg1) should be untouched
    assert result[-3]["content"] == "msg1"
    # index -1 (msg3) should be untouched
    assert result[-1]["content"] == "msg3"


# ---------------------------------------------------------------------------
# Integration: complete() passes cached system and tools to the API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_passes_system_as_list_with_cache_control():
    backend = make_backend()
    mock_response = MagicMock()
    mock_response.content = []
    backend.client.messages.create = AsyncMock(return_value=mock_response)

    from elasticity.backends.anthropic import AnthropicBackend
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    await backend.complete("claude-3-5-sonnet-20241022", messages)

    call_kwargs = backend.client.messages.create.call_args[1]
    system_param = call_kwargs["system"]
    assert isinstance(system_param, list)
    assert system_param[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_complete_passes_tools_with_cache_control_on_last():
    backend = make_backend()
    mock_response = MagicMock()
    mock_response.content = []
    backend.client.messages.create = AsyncMock(return_value=mock_response)

    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    tools = _make_tools("search", "read")
    await backend.complete("claude-3-5-sonnet-20241022", messages, tools=tools)

    call_kwargs = backend.client.messages.create.call_args[1]
    api_tools = call_kwargs["tools"]
    assert "cache_control" not in api_tools[0]
    assert api_tools[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_complete_empty_system_passes_string():
    backend = make_backend()
    mock_response = MagicMock()
    mock_response.content = []
    backend.client.messages.create = AsyncMock(return_value=mock_response)

    messages = [{"role": "user", "content": "Hello"}]
    await backend.complete("claude-3-5-sonnet-20241022", messages)

    call_kwargs = backend.client.messages.create.call_args[1]
    assert call_kwargs["system"] == ""


# ---------------------------------------------------------------------------
# _build_anthropic_messages — secondary system message demotion
# ---------------------------------------------------------------------------


def test_single_system_message_captured_unchanged():
    backend = make_backend()
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    system, out = backend._build_anthropic_messages(messages)
    assert system == "You are helpful."
    assert len(out) == 1
    assert out[0] == {"role": "user", "content": "Hello"}


def test_secondary_system_messages_not_in_system_string():
    backend = make_backend()
    messages = [
        {"role": "system", "content": "Main system prompt."},
        {"role": "system", "content": "[Context note: topic shifted]"},
        {"role": "user", "content": "Current input"},
    ]
    system, out = backend._build_anthropic_messages(messages)
    # Static system prompt must remain byte-identical
    assert system == "Main system prompt."
    assert "[Context note" not in system


def test_secondary_system_messages_prepended_to_last_user_message():
    backend = make_backend()
    messages = [
        {"role": "system", "content": "Main prompt."},
        {"role": "system", "content": "[Context note: topic shifted]"},
        {"role": "system", "content": "[Recalled context: some memory]"},
        {"role": "user", "content": "Prior turn"},
        {"role": "assistant", "content": [{"type": "text", "text": "Prior reply"}]},
        {"role": "user", "content": "Current input"},
    ]
    system, out = backend._build_anthropic_messages(messages)
    assert system == "Main prompt."
    # Last message should be the current user input with context notes prepended
    last = out[-1]
    assert last["role"] == "user"
    assert "[Context note: topic shifted]" in last["content"]
    assert "[Recalled context: some memory]" in last["content"]
    assert "Current input" in last["content"]
    # Context notes should appear BEFORE the user input
    assert last["content"].index("[Context note") < last["content"].index("Current input")


def test_system_string_stable_across_shift_and_non_shift_turns():
    backend = make_backend()
    no_shift = [
        {"role": "system", "content": "Main prompt."},
        {"role": "user", "content": "Normal turn"},
    ]
    with_shift = [
        {"role": "system", "content": "Main prompt."},
        {"role": "system", "content": "[Context note: topic shifted]"},
        {"role": "user", "content": "Shift turn"},
    ]
    system_a, _ = backend._build_anthropic_messages(no_shift)
    system_b, _ = backend._build_anthropic_messages(with_shift)
    assert system_a == system_b


def test_multiple_secondary_system_messages_joined_in_order():
    backend = make_backend()
    messages = [
        {"role": "system", "content": "Prompt."},
        {"role": "system", "content": "Note A"},
        {"role": "system", "content": "Note B"},
        {"role": "user", "content": "Input"},
    ]
    _, out = backend._build_anthropic_messages(messages)
    content = out[-1]["content"]
    assert content.index("Note A") < content.index("Note B")
    assert content.index("Note B") < content.index("Input")


def test_no_secondary_system_messages_leaves_user_content_intact():
    backend = make_backend()
    messages = [
        {"role": "system", "content": "Prompt."},
        {"role": "user", "content": "Prior"},
        {"role": "assistant", "content": [{"type": "text", "text": "Reply"}]},
        {"role": "user", "content": "Current"},
    ]
    _, out = backend._build_anthropic_messages(messages)
    assert out[-1]["content"] == "Current"
