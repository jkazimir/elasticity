"""Anthropic backend implementation."""

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from .base import Backend, CompletionResponse, StreamChunk, ToolCall, ToolSpec, Usage
from ..errors import BackendError

logger = logging.getLogger(__name__)

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None


class AnthropicBackend(Backend):
    """Anthropic backend using the anthropic SDK.

    Configure via environment variables:
    - ANTHROPIC_API_KEY: API key (required)
    """

    def __init__(self):
        if AsyncAnthropic is None:
            raise BackendError(
                "The 'anthropic' package is required for the anthropic backend.\n"
                "Install it with: pip install elasticity[anthropic]"
            )

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendError("ANTHROPIC_API_KEY environment variable is required")

        self.client = AsyncAnthropic(api_key=api_key)

    def _build_anthropic_messages(
        self, messages: List[Dict[str, Any]]
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Extract system prompt and convert messages to Anthropic format.

        Only the first system-role message (the static agent system prompt) is
        used as the system param. Any subsequent system-role messages — injected
        by CognitiveStrategy as per-turn context notes (topic-shift annotations,
        recalled memories) — are buffered and prepended to the final user message.
        This keeps the system string byte-identical across turns for cache stability
        and places the context notes at the chronologically correct position (right
        before the current user input) rather than appending them to the main prompt
        where they would erode the recency of the process rules that drive tool-call
        decisions.
        """
        system_message = ""
        anthropic_messages = []
        first_system_seen = False
        deferred_context: List[str] = []

        for msg in messages:
            if msg.get("role") == "system":
                if not first_system_seen:
                    system_message = msg.get("content", "")
                    first_system_seen = True
                else:
                    content = msg.get("content", "")
                    if content:
                        deferred_context.append(content)
            elif msg.get("role") == "tool":
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id"),
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )
            elif msg.get("role") == "assistant":
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", [])
                anthropic_content = []
                if content:
                    anthropic_content.append({"type": "text", "text": content})
                for tc in tool_calls:
                    anthropic_content.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        }
                    )
                anthropic_messages.append({"role": "assistant", "content": anthropic_content})
            else:
                role = msg.get("role", "user")
                if role != "user":
                    raise BackendError(f"Unsupported message role for Anthropic: {role}")
                content = msg.get("content", "")
                anthropic_messages.append({"role": "user", "content": content})

        # Prepend buffered context notes to the last user message so they land
        # directly before the current input rather than being lost or misplaced.
        if deferred_context:
            prefix = "\n\n".join(deferred_context)
            for i in range(len(anthropic_messages) - 1, -1, -1):
                m = anthropic_messages[i]
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    anthropic_messages[i] = {
                        **m,
                        "content": f"{prefix}\n\n{m['content']}" if m["content"] else prefix,
                    }
                    break

        return system_message, anthropic_messages

    def _build_system_param(self, system_message: str) -> Union[str, List[Dict[str, Any]]]:
        """Convert system message string to Anthropic system param with cache annotation.

        Returns a plain string for empty prompts (no-op) or a list of content blocks
        with a cache breakpoint for non-empty prompts, enabling prompt caching.
        """
        if not system_message:
            return ""
        return [
            {
                "type": "text",
                "text": system_message,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _build_anthropic_tools(
        self, tools: Optional[List[ToolSpec]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Convert ToolSpec list to Anthropic tool format with cache annotation on the last tool.

        Adding cache_control to the last tool tells the API to cache everything from
        the start of the request through all tool definitions.
        """
        if not tools:
            return None
        anthropic_tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in tools
        ]
        anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
        return anthropic_tools

    def _annotate_message_cache_breakpoint(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Add a cache breakpoint to the second-to-last message.

        This caches the entire conversation prefix (system + tools + all prior turns)
        up to but not including the latest user input. Particularly effective during
        multi-round tool loops where the message list grows each round.

        Returns the original list unchanged if fewer than 2 messages are present.
        Does not mutate the caller's data.
        """
        if len(messages) < 2:
            return messages

        messages = [dict(m) for m in messages]
        target = messages[-2]
        content = target.get("content")

        if isinstance(content, str) and content:
            target["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        elif isinstance(content, list) and content:
            last_block = dict(content[-1])
            last_block["cache_control"] = {"type": "ephemeral"}
            target["content"] = list(content[:-1]) + [last_block]

        return messages

    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]] = None,
        max_tokens: int = 4096,
        response_format: Optional[Dict[str, Any]] = None,  # noqa: ARG002 - not supported by Anthropic API; prompt injection handles JSON formatting
    ) -> CompletionResponse:
        """Complete a chat conversation using Anthropic API."""
        system_message, anthropic_messages = self._build_anthropic_messages(messages)
        anthropic_messages = self._annotate_message_cache_breakpoint(anthropic_messages)
        anthropic_tools = self._build_anthropic_tools(tools)

        try:
            create_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": anthropic_messages,
                "system": self._build_system_param(system_message),
                "max_tokens": max_tokens,
            }
            if anthropic_tools:
                create_kwargs["tools"] = anthropic_tools
                create_kwargs["tool_choice"] = {"type": "auto"}

            response = await self.client.messages.create(**create_kwargs)

            # Parse Anthropic response format
            content_parts = []
            tool_calls = []

            for block in response.content:
                if block.type == "text":
                    content_parts.append(block.text)
                elif block.type == "tool_use":
                    # Parse tool_use arguments
                    args = block.input if isinstance(block.input, dict) else {}
                    tool_calls.append(
                        ToolCall(
                            id=block.id,
                            name=block.name,
                            arguments=args,
                        )
                    )

            content = "".join(content_parts)

            usage = Usage(
                input_tokens=getattr(response.usage, "input_tokens", 0),
                output_tokens=getattr(response.usage, "output_tokens", 0),
                cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0),
                cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0),
            )

            return CompletionResponse(content=content, tool_calls=tool_calls, stop_reason=response.stop_reason, usage=usage)

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(f"Anthropic API call failed: {e}") from e

    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]] = None,
        max_tokens: int = 4096,
        response_format: Optional[Dict[str, Any]] = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion using Anthropic's native streaming API."""
        system_message, anthropic_messages = self._build_anthropic_messages(messages)
        anthropic_messages = self._annotate_message_cache_breakpoint(anthropic_messages)
        anthropic_tools = self._build_anthropic_tools(tools)

        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "system": self._build_system_param(system_message),
            "max_tokens": max_tokens,
        }
        if anthropic_tools:
            create_kwargs["tools"] = anthropic_tools
            create_kwargs["tool_choice"] = {"type": "auto"}

        try:
            # Accumulate tool call input fragments per block index
            pending_tool_calls: Dict[int, Dict[str, Any]] = {}
            stop_reason: Optional[str] = None
            input_tokens = 0
            output_tokens = 0
            cache_read_tokens = 0
            cache_creation_tokens = 0

            async with self.client.messages.stream(**create_kwargs) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", None)

                    if event_type == "message_start":
                        msg = getattr(event, "message", None)
                        if msg and hasattr(msg, "usage"):
                            input_tokens = getattr(msg.usage, "input_tokens", 0)
                            cache_read_tokens = getattr(msg.usage, "cache_read_input_tokens", 0)
                            cache_creation_tokens = getattr(msg.usage, "cache_creation_input_tokens", 0)

                    elif event_type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            pending_tool_calls[event.index] = {
                                "id": block.id,
                                "name": block.name,
                                "input_json": "",
                            }

                    elif event_type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield StreamChunk(delta=delta.text)
                        elif delta.type == "input_json_delta":
                            if event.index in pending_tool_calls:
                                pending_tool_calls[event.index]["input_json"] += delta.partial_json

                    elif event_type == "message_delta":
                        if hasattr(event, "delta") and hasattr(event.delta, "stop_reason"):
                            stop_reason = event.delta.stop_reason
                        if hasattr(event, "usage"):
                            output_tokens = getattr(event.usage, "output_tokens", 0)

                    elif event_type == "message_stop":
                        break

            truncated = stop_reason == "max_tokens"

            # Yield completed tool calls
            for tc_data in pending_tool_calls.values():
                try:
                    args = json.loads(tc_data["input_json"]) if tc_data["input_json"] else {}
                except json.JSONDecodeError:
                    if truncated:
                        logger.warning(
                            "Tool call JSON truncated due to max_tokens limit; skipping tool call. tool=%s",
                            tc_data.get("name"),
                        )
                        continue
                    logger.warning(
                        "Malformed tool call JSON from Anthropic stream; calling tool with empty args. "
                        "raw=%r tool=%s",
                        tc_data["input_json"],
                        tc_data.get("name"),
                    )
                    args = {}
                yield StreamChunk(
                    tool_call=ToolCall(
                        id=tc_data["id"],
                        name=tc_data["name"],
                        arguments=args,
                    )
                )

            usage = Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
            )
            yield StreamChunk(done=True, truncated=truncated, usage=usage)

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(f"Anthropic streaming API call failed: {e}") from e
