"""OpenAI-compatible backend implementation."""

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from .base import Backend, CompletionResponse, StreamChunk, ToolCall, ToolSpec, Usage
from ..errors import BackendError

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None


class OpenAIBackend(Backend):
    """OpenAI-compatible backend using the openai SDK.

    Works with OpenAI, Ollama, vLLM, and any other OpenAI-compatible API.
    Configure via environment variables:
    - OPENAI_API_KEY: API key (required)
    - OPENAI_BASE_URL: Base URL for custom endpoints (optional, defaults to OpenAI)
    """

    def __init__(self):
        if AsyncOpenAI is None:
            raise BackendError(
                "The 'openai' package is required for the openai backend.\n"
                "Install it with: pip install elasticity[openai]"
            )

        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")

        # Local endpoints (e.g. Ollama) don't require a real API key, but the
        # OpenAI SDK always demands a non-null value.  Use a placeholder when a
        # custom base URL is set and no key is configured.
        if not api_key and base_url:
            api_key = "ollama"

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]] = None,
        max_tokens: int = 4096,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> CompletionResponse:
        """Complete a chat conversation using OpenAI-compatible API."""
        # Convert ToolSpec to OpenAI function-calling format
        openai_tools = None
        if tools:
            openai_tools = []
            for tool in tools:
                openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        },
                    }
                )

        try:
            # Convert messages to OpenAI format, handling tool_calls metadata
            openai_messages = []
            for msg in messages:
                openai_msg = {"role": msg["role"], "content": msg.get("content", "")}
                
                # Handle assistant messages with tool_calls metadata
                if msg["role"] == "assistant" and "tool_calls" in msg:
                    # Convert tool_calls metadata to OpenAI format
                    tool_calls_list = []
                    for tc in msg["tool_calls"]:
                        tool_calls_list.append(
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"],
                                },
                            }
                        )
                    openai_msg["tool_calls"] = tool_calls_list
                
                # Handle tool result messages
                if msg["role"] == "tool":
                    openai_msg["tool_call_id"] = msg.get("tool_call_id")
                    openai_msg["content"] = msg.get("content", "")
                
                openai_messages.append(openai_msg)
            
            create_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": openai_messages,
                "max_tokens": max_tokens,
            }
            if openai_tools:
                create_kwargs["tools"] = openai_tools
                create_kwargs["tool_choice"] = "auto"
            if response_format:
                create_kwargs["response_format"] = response_format

            response = await self.client.chat.completions.create(**create_kwargs)

            choice = response.choices[0]
            message = choice.message

            content = message.content or ""
            tool_calls = []

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    # Parse arguments JSON if needed
                    args = tool_call.function.arguments
                    if isinstance(args, str):
                        import json

                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            raise BackendError(f"Invalid tool arguments JSON: {args}")

                    tool_calls.append(
                        ToolCall(
                            id=tool_call.id,
                            name=tool_call.function.name,
                            arguments=args,
                        )
                    )

            usage = None
            if response.usage:
                usage = Usage(
                    input_tokens=response.usage.prompt_tokens or 0,
                    output_tokens=response.usage.completion_tokens or 0,
                )

            return CompletionResponse(content=content, tool_calls=tool_calls, stop_reason=choice.finish_reason, usage=usage)

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(f"OpenAI API call failed: {e}") from e

    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]] = None,
        max_tokens: int = 4096,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion using OpenAI's native streaming API."""
        openai_tools = None
        if tools:
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]

        openai_messages = []
        for msg in messages:
            openai_msg = {"role": msg["role"], "content": msg.get("content", "")}
            if msg["role"] == "assistant" and "tool_calls" in msg:
                tool_calls_list = []
                for tc in msg["tool_calls"]:
                    tool_calls_list.append(
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"])
                                if isinstance(tc["arguments"], dict)
                                else tc["arguments"],
                            },
                        }
                    )
                openai_msg["tool_calls"] = tool_calls_list
            if msg["role"] == "tool":
                openai_msg["tool_call_id"] = msg.get("tool_call_id")
                openai_msg["content"] = msg.get("content", "")
            openai_messages.append(openai_msg)

        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
        }
        if openai_tools:
            create_kwargs["tools"] = openai_tools
            create_kwargs["tool_choice"] = "auto"
        if response_format:
            create_kwargs["response_format"] = response_format

        try:
            # Accumulate tool call fragments indexed by their position in the response
            pending_tool_calls: Dict[int, Dict[str, Any]] = {}
            final_finish_reason: Optional[str] = None
            stream_usage: Optional[Usage] = None

            async for chunk in await self.client.chat.completions.create(**create_kwargs, stream=True):
                # Capture usage from chunks that carry it (typically the final one)
                if getattr(chunk, "usage", None):
                    stream_usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    )

                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue

                delta = choice.delta

                # Text delta
                if delta.content:
                    yield StreamChunk(delta=delta.content)

                # Tool call fragments -- accumulate until complete
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": tc_delta.function.name or "" if tc_delta.function else "",
                                "arguments": "",
                            }
                        else:
                            if tc_delta.id:
                                pending_tool_calls[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    pending_tool_calls[idx]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    pending_tool_calls[idx]["arguments"] += tc_delta.function.arguments

                if choice.finish_reason in ("tool_calls", "stop", "length"):
                    final_finish_reason = choice.finish_reason
                    break

            truncated = final_finish_reason == "length"

            # Yield completed tool calls
            for tc_data in pending_tool_calls.values():
                try:
                    args = json.loads(tc_data["arguments"]) if tc_data["arguments"] else {}
                except json.JSONDecodeError:
                    if truncated:
                        logger.warning(
                            "Tool call JSON truncated due to max_tokens limit; skipping tool call. tool=%s",
                            tc_data.get("name"),
                        )
                        continue
                    logger.warning(
                        "Malformed tool call JSON from OpenAI stream; calling tool with empty args. "
                        "raw=%r tool=%s",
                        tc_data["arguments"],
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

            yield StreamChunk(done=True, truncated=truncated, usage=stream_usage)

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(f"OpenAI streaming API call failed: {e}") from e
