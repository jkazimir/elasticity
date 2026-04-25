"""Input handling for concurrent user input during orchestration execution."""

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..config.schema import InputHandlingConfig
from ..events import EventBus, InputQueueFull


@dataclass
class UserInput:
    """Represents user input received during execution."""

    message: str
    timestamp: float
    is_interrupt: bool = False


class InputHandler:
    """Manages concurrent user input during orchestration execution.

    Supports queueing messages for processing after the current turn,
    and interrupt signaling for immediate handling (cancel or graceful).
    """

    def __init__(
        self,
        config: InputHandlingConfig,
        event_bus: Optional[EventBus] = None,
    ):
        self.config = config
        self._events = event_bus or EventBus()
        self._queue: asyncio.Queue[UserInput] = asyncio.Queue(maxsize=config.queue_limit)
        self._interrupt_event = asyncio.Event()
        self._current_interrupt: Optional[UserInput] = None
        self._lock = threading.Lock()

    def submit(self, message: str, is_interrupt: bool = False) -> bool:
        """Submit user input.

        For queue mode: adds to queue (returns False if full).
        For interrupt mode: signals interrupt and stores message.
        For ignore mode: discards (returns True as no-op).

        Returns:
            True if accepted, False if queue full (queue mode only).
        """
        if self.config.mode == "ignore":
            return True

        user_input = UserInput(
            message=message.strip(),
            timestamp=time.monotonic(),
            is_interrupt=is_interrupt,
        )

        if is_interrupt and self.config.mode == "interrupt":
            with self._lock:
                self._current_interrupt = user_input
            self._interrupt_event.set()
            return True

        if self.config.mode == "queue" and not is_interrupt:
            try:
                self._queue.put_nowait(user_input)
                return True
            except asyncio.QueueFull:
                self._events.emit(InputQueueFull(
                    message=message[:100],
                    queue_depth=self.config.queue_limit,
                ))
                return False

        return True

    def request_interrupt(self, message: str) -> None:
        """Signal an interrupt with the given message."""
        self.submit(message, is_interrupt=True)

    async def poll_queue(self) -> Optional[UserInput]:
        """Non-blocking poll for the next queued message."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def has_interrupt(self) -> bool:
        """Check if an interrupt has been requested."""
        return self._interrupt_event.is_set()

    def peek_interrupt(self) -> Optional[UserInput]:
        """Peek at current interrupt without consuming. Returns None if no interrupt."""
        with self._lock:
            return self._current_interrupt

    def get_interrupt(self) -> Optional[UserInput]:
        """Get and clear the current interrupt message. Safe to call from async context."""
        with self._lock:
            ui = self._current_interrupt
            self._current_interrupt = None
        self._interrupt_event.clear()
        return ui

    def queue_depth(self) -> int:
        """Return current number of queued messages."""
        return self._queue.qsize()

    def drain_queue(self) -> list[UserInput]:
        """Drain all queued messages. Returns list of UserInput."""
        result: list[UserInput] = []
        while True:
            try:
                result.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return result

    def clear(self) -> None:
        """Clear queue and interrupt state."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        with self._lock:
            self._current_interrupt = None
        self._interrupt_event.clear()
