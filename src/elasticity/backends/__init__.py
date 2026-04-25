"""Backend abstraction layer for LLM providers."""

from .registry import resolve_backend
from .base import Backend, CompletionResponse, ToolCall, ToolSpec, Usage

__all__ = ["Backend", "CompletionResponse", "ToolCall", "ToolSpec", "Usage", "resolve_backend"]
