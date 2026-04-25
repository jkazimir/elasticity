"""Structured event system for the Elasticity runtime.

The EventBus decouples the execution engine from presentation concerns
(streaming display, observability, session persistence, tracing). Producers
emit typed events; consumers subscribe to specific event types.

All callbacks are invoked synchronously during emit. Subscribers must not
raise exceptions -- any that do are silently swallowed to protect the runtime.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Base class for all runtime events."""

    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Orchestration lifecycle
# ---------------------------------------------------------------------------


@dataclass
class OrchestrationStarted(Event):
    run_id: str = ""
    orchestration_name: str = ""


@dataclass
class OrchestrationCompleted(Event):
    run_id: str = ""
    orchestration_name: str = ""
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Input handling (concurrent input during execution)
# ---------------------------------------------------------------------------


@dataclass
class InputQueueFull(Event):
    """Emitted when the input queue is full and a new message is rejected."""

    message: str = ""
    queue_depth: int = 0


@dataclass
class InterruptReceived(Event):
    """Emitted when an interrupt is detected during orchestration execution."""

    message: str = ""
    orchestration: str = ""
    current_node: str = ""
    behavior: str = ""  # "cancel" or "graceful"


@dataclass
class OrchestrationInterruptedEvent(Event):
    """Emitted when orchestration is cancelled due to an interrupt.

    Note: This is an event, not the OrchestrationInterrupted exception.
    """

    orchestration: str = ""
    interrupted_at_node: str = ""
    interrupt_message: str = ""


# ---------------------------------------------------------------------------
# Node lifecycle (internal graph nodes)
# ---------------------------------------------------------------------------


@dataclass
class NodeStarted(Event):
    step_id: str = ""
    node_type: str = ""


@dataclass
class NodeCompleted(Event):
    step_id: str = ""


@dataclass
class NodeError(Event):
    step_id: str = ""
    error: str = ""


@dataclass
class NodeRetrying(Event):
    step_id: str = ""
    attempt: int = 0
    delay_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Agent events
# ---------------------------------------------------------------------------


@dataclass
class AgentStarted(Event):
    step_id: str = ""
    agent_name: str = ""
    input_text: str = ""


@dataclass
class AgentToken(Event):
    """Emitted for each streaming text token from an agent."""

    step_id: str = ""
    agent_name: str = ""
    token: str = ""


@dataclass
class AgentCompleted(Event):
    step_id: str = ""
    agent_name: str = ""
    output: str = ""
    duration_ms: float = 0.0
    # Backend completion metadata (for observability / calibration diagnostics)
    stop_reason: str = ""
    output_chars: int = 0
    truncation_recoveries: int = 0
    # Token usage from the last LLM API call
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class AgentErrorEvent(Event):
    step_id: str = ""
    agent_name: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Tool events
# ---------------------------------------------------------------------------


@dataclass
class ToolCalled(Event):
    step_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult(Event):
    step_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    result: str = ""
    duration_ms: float = 0.0


@dataclass
class ToolApprovalRequested(Event):
    """Emitted when a tool call requires user approval before execution."""

    step_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDenied(Event):
    """Emitted when a tool call is blocked by policy or user rejection."""

    step_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    reason: str = ""  # "policy" | "user_denied"


@dataclass
class ToolCallStepStarted(Event):
    """Emitted before a flow-level tool_call invocation (zero-LLM-cost)."""

    step_id: str = ""
    tool_name: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallStepCompleted(Event):
    """Emitted after a flow-level tool_call invocation completes."""

    step_id: str = ""
    tool_name: str = ""
    result: str = ""
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Flow primitive events
# ---------------------------------------------------------------------------


@dataclass
class LoopIteration(Event):
    step_id: str = ""
    iteration: int = 0


@dataclass
class RouteTaken(Event):
    step_id: str = ""
    case: str = ""


@dataclass
class ParallelStarted(Event):
    step_id: str = ""
    branch_count: int = 0


@dataclass
class ParallelCompleted(Event):
    step_id: str = ""


@dataclass
class SpawnStarted(Event):
    parent_agent: str = ""
    child_type: str = ""
    child_id: str = ""


@dataclass
class SpawnCompleted(Event):
    child_id: str = ""
    child_type: str = ""


@dataclass
class SpawnParseFailed(Event):
    parent_agent: str = ""
    content_preview: str = ""


@dataclass
class SpawnWaveStarted(Event):
    parent_agent: str = ""
    wave_index: int = 0
    wave_count: int = 0
    spawn_count: int = 0


@dataclass
class SpawnWaveCompleted(Event):
    parent_agent: str = ""
    wave_index: int = 0
    wave_count: int = 0


# ---------------------------------------------------------------------------
# Supervisor events
# ---------------------------------------------------------------------------


@dataclass
class SupervisorWorkerStarted(Event):
    worker_id: str = ""
    worker_agent: str = ""
    attempt: int = 0


@dataclass
class SupervisorReview(Event):
    supervisor: str = ""
    worker_id: str = ""
    attempt: int = 0


@dataclass
class SupervisorAccepted(Event):
    supervisor: str = ""
    worker_id: str = ""
    attempt: int = 0


@dataclass
class SupervisorRejected(Event):
    supervisor: str = ""
    worker_id: str = ""
    attempt: int = 0
    feedback: Optional[str] = None


# ---------------------------------------------------------------------------
# Approval events (human-in-the-loop)
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequested(Event):
    step_id: str = ""
    content: str = ""
    message: str = ""
    attempt: int = 0


@dataclass
class ApprovalGranted(Event):
    step_id: str = ""
    attempt: int = 0


@dataclass
class ApprovalRejected(Event):
    step_id: str = ""
    attempt: int = 0
    feedback: str = ""


@dataclass
class ApprovalEdited(Event):
    step_id: str = ""
    attempt: int = 0


# ---------------------------------------------------------------------------
# Cognitive context events
# ---------------------------------------------------------------------------


@dataclass
class TopicShift(Event):
    """Emitted when the cognitive strategy detects a conversational topic shift."""

    from_topic: str = ""
    to_topic: str = ""


@dataclass
class MemoryRecalled(Event):
    """Emitted when RAG recall injects memories into context."""

    count: int = 0
    memory_keys: str = ""


@dataclass
class ContextAssembled(Event):
    """Emitted after cognitive context is fully assembled for an LLM call."""

    total_messages: int = 0
    recalled_memories: int = 0
    recent_turns: int = 0
    topic: str = ""


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


class EventBus:
    """Simple synchronous publish-subscribe event bus.

    Subscribers register for a specific event type and are called for any
    emitted event that is an instance of that type (including subclasses).

    Example::

        bus = EventBus()
        bus.subscribe(AgentStarted, lambda e: print(f"Agent started: {e.agent_name}"))
        bus.emit(AgentStarted(agent_name="researcher", step_id="step_1"))
    """

    def __init__(self) -> None:
        self._subscribers: Dict[Type[Event], List[Callable[[Event], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: Type[Event], callback: Callable[[Event], None]) -> None:
        """Register a callback for events of the given type (and subclasses)."""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: Type[Event], callback: Callable[[Event], None]) -> None:
        """Remove a previously registered callback. No-op if not found."""
        with self._lock:
            try:
                self._subscribers[event_type].remove(callback)
            except (KeyError, ValueError):
                pass

    def emit(self, event: Event) -> None:
        """Dispatch an event to all matching subscribers.

        Never raises -- subscriber exceptions are silently ignored to protect
        the runtime from misbehaving consumers.
        """
        event_cls = type(event)
        # Snapshot the subscriber dict under the lock so that concurrent
        # subscribe/unsubscribe calls don't cause "dict changed size during
        # iteration" errors.
        with self._lock:
            snapshot = {t: list(cbs) for t, cbs in self._subscribers.items()}
        for registered_type, callbacks in snapshot.items():
            if issubclass(event_cls, registered_type):
                for callback in callbacks:
                    try:
                        callback(event)
                    except Exception:
                        logger.debug("EventBus subscriber raised on %s", type(event).__name__, exc_info=True)
