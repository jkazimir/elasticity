"""Base backend interface and data types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


@dataclass
class ToolSpec:
    """Normalized tool specification for function calling."""

    name: str
    description: str
    parameters: Dict[str, Any]


@dataclass
class ToolCall:
    """Normalized tool call from LLM response."""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class Usage:
    """Token usage metadata from an LLM API response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class CompletionResponse:
    """Normalized completion response from any backend."""

    content: str
    tool_calls: List[ToolCall]
    stop_reason: Optional[str] = None
    usage: Optional[Usage] = None


@dataclass
class StreamChunk:
    """A single chunk from a streaming LLM response.

    Either ``delta`` contains a text fragment, or ``tool_call`` contains a
    complete tool call that was accumulated from the stream. ``done`` is True
    on the final chunk. ``truncated`` is True on the final chunk when the
    response was cut off by the max_tokens limit.
    """

    delta: str = ""
    tool_call: Optional[ToolCall] = None
    done: bool = False
    truncated: bool = False
    usage: Optional[Usage] = None


class Backend(ABC):
    """Abstract base class for LLM backends."""

    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]] = None,
        max_tokens: int = 4096,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> CompletionResponse:
        """Complete a chat conversation.

        Args:
            model: Model identifier (without provider prefix)
            messages: List of message dicts with 'role' and 'content' keys
            tools: Optional list of tool specifications
            max_tokens: Maximum tokens in the response
            response_format: Optional response format hint (e.g. {"type": "json_object"}).
                Backends that support it (e.g. OpenAI) will enforce the format at the API
                level; others will silently ignore it and rely on prompt injection alone.

        Returns:
            Normalized completion response

        Raises:
            BackendError: If the backend SDK is not installed or API call fails
        """
        pass

    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolSpec]] = None,
        max_tokens: int = 4096,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion, yielding chunks as they arrive.

        The default implementation falls back to ``complete()`` and yields a
        single chunk containing the full response. Backends that support native
        streaming should override this method for token-by-token delivery.

        Tool calls are yielded as complete ``StreamChunk(tool_call=...)`` objects
        once the full call has been accumulated from the stream.

        Args:
            model: Model identifier (without provider prefix)
            messages: List of message dicts
            tools: Optional list of tool specifications
            max_tokens: Maximum tokens in the response
            response_format: Optional response format hint

        Yields:
            StreamChunk objects (text deltas and/or complete tool calls)
        """
        response = await self.complete(model, messages, tools, max_tokens, response_format)
        if response.content:
            yield StreamChunk(delta=response.content, done=False)
        for tool_call in response.tool_calls:
            yield StreamChunk(tool_call=tool_call, done=False)
        yield StreamChunk(done=True, truncated=(response.stop_reason == "max_tokens"))
