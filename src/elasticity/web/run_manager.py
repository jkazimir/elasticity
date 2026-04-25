"""Active-run registry for the web server.

Each in-flight orchestration run owns an :class:`ActiveRun` that holds:

* An async queue that SSE generators read from.
* A pending ``asyncio.Future`` used to ferry approval / ask_user answers from
  the HTTP layer back into the blocked orchestration coroutine.
* A task → run-id mapping so a module-level ``ask_user`` dispatcher can route
  calls to the right run even with multiple concurrent runs.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Sentinel placed in sse_queue to signal end-of-stream.
_SSE_DONE = object()


@dataclass
class ActiveRun:
    run_id: str
    sse_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))

    # Set while the orchestration coroutine is blocked waiting for human input.
    pending_future: Optional[asyncio.Future] = None
    # "approval" | "ask_user" | "human_approval"
    pending_type: Optional[str] = None

    # Per-session tool-approval policies (mirrors the CLI ChatSessionState pattern).
    session_policies: Dict[str, str] = field(default_factory=dict)

    def finish(self) -> None:
        """Signal end-of-stream to the SSE generator."""
        try:
            self.sse_queue.put_nowait(_SSE_DONE)
        except asyncio.QueueFull:
            pass

    def emit(self, event_str: str) -> None:
        """Non-blocking event push — drops silently when queue is full."""
        try:
            self.sse_queue.put_nowait(event_str)
        except asyncio.QueueFull:
            pass

    async def ask_user(self, question: str) -> str:
        """Block the orchestration task and wait for a web-submitted answer."""
        loop = asyncio.get_running_loop()
        self.pending_future = loop.create_future()
        self.pending_type = "ask_user"
        try:
            return await self.pending_future
        finally:
            self.pending_future = None
            self.pending_type = None


class RunManager:
    """Thread-safe (asyncio) registry of active runs."""

    def __init__(self) -> None:
        self._runs: Dict[str, ActiveRun] = {}
        # Maps asyncio Task → run_id so the global ask_user dispatcher can
        # route to the correct ActiveRun without storing state in the global.
        self._task_run: Dict[asyncio.Task, str] = {}

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create(self) -> ActiveRun:
        run_id = str(uuid.uuid4())
        run = ActiveRun(run_id=run_id)
        self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> Optional[ActiveRun]:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    # ------------------------------------------------------------------
    # Task ↔ run mapping (for ask_user dispatch)
    # ------------------------------------------------------------------

    def register_task(self, task: asyncio.Task, run_id: str) -> None:
        self._task_run[task] = run_id
        # Auto-unregister when the task finishes.
        task.add_done_callback(lambda t: self._task_run.pop(t, None))

    def get_run_for_current_task(self) -> Optional[ActiveRun]:
        task = asyncio.current_task()
        if task is None:
            return None
        run_id = self._task_run.get(task)
        return self._runs.get(run_id) if run_id else None

    # ------------------------------------------------------------------
    # SSE generator
    # ------------------------------------------------------------------

    async def sse_generator(self, run_id: str):
        """Async generator that yields SSE-formatted strings until end-of-stream."""
        run = self.get(run_id)
        if run is None:
            return
        try:
            while True:
                item = await run.sse_queue.get()
                if item is _SSE_DONE:
                    break
                yield item
        finally:
            self.remove(run_id)
