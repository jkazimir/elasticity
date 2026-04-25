"""Built-in ask_user tool — allows agents to ask the user clarifying questions."""

from typing import Awaitable, Callable, Optional

_ask_user_fn: Optional[Callable[[str], Awaitable[str]]] = None


def set_ask_user_fn(fn: Optional[Callable[[str], Awaitable[str]]]) -> None:
    """Set the callback used to ask the user a question."""
    global _ask_user_fn
    _ask_user_fn = fn


async def ask(question: str) -> str:
    """Ask the user a clarifying question and return their answer.

    Falls back to a static message when no interactive callback is registered
    (e.g., in batch/test mode).
    """
    if _ask_user_fn is None:
        return "[ask_user not available in this context]"
    return await _ask_user_fn(question)
