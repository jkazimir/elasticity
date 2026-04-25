"""Tests for InputHandler and concurrent input handling."""

import asyncio

import pytest

from elasticity.config.schema import InputHandlingConfig
from elasticity.events import EventBus, InputQueueFull
from elasticity.runtime.input_handler import InputHandler, UserInput


def test_user_input_dataclass():
    """UserInput stores message, timestamp, is_interrupt."""
    ui = UserInput(message="hello", timestamp=1.0, is_interrupt=False)
    assert ui.message == "hello"
    assert ui.timestamp == 1.0
    assert ui.is_interrupt is False


def test_input_handler_queue_mode_submit():
    """Queue mode accepts messages and stores in queue."""
    config = InputHandlingConfig(mode="queue", queue_limit=5)
    handler = InputHandler(config)
    assert handler.submit("msg1") is True
    assert handler.submit("msg2") is True
    assert handler.queue_depth() == 2


async def test_input_handler_queue_mode_poll():
    """Queue mode returns messages in order via poll_queue."""
    config = InputHandlingConfig(mode="queue", queue_limit=5)
    handler = InputHandler(config)
    handler.submit("first")
    handler.submit("second")
    ui1 = await handler.poll_queue()
    assert ui1 is not None
    assert ui1.message == "first"
    ui2 = await handler.poll_queue()
    assert ui2 is not None
    assert ui2.message == "second"
    ui3 = await handler.poll_queue()
    assert ui3 is None


def test_input_handler_queue_overflow():
    """Queue mode rejects when full and emits InputQueueFull."""
    config = InputHandlingConfig(mode="queue", queue_limit=2)
    bus = EventBus()
    received = []

    def on_full(e):
        received.append(e)

    bus.subscribe(InputQueueFull, on_full)
    handler = InputHandler(config, event_bus=bus)
    assert handler.submit("msg1") is True
    assert handler.submit("msg2") is True
    assert handler.submit("msg3") is False
    assert len(received) == 1
    assert received[0].queue_depth == 2


def test_input_handler_interrupt_mode():
    """Interrupt mode stores interrupt and signals."""
    config = InputHandlingConfig(mode="interrupt", interrupt_behavior="cancel")
    handler = InputHandler(config)
    handler.request_interrupt("stop")
    assert handler.has_interrupt() is True
    ui = handler.get_interrupt()
    assert ui is not None
    assert ui.message == "stop"
    assert ui.is_interrupt is True
    assert handler.has_interrupt() is False


def test_input_handler_ignore_mode():
    """Ignore mode discards all input."""
    config = InputHandlingConfig(mode="ignore")
    handler = InputHandler(config)
    assert handler.submit("ignored") is True
    assert handler.queue_depth() == 0
    assert handler.has_interrupt() is False


def test_input_handler_drain_queue():
    """drain_queue returns all queued messages."""
    config = InputHandlingConfig(mode="queue", queue_limit=5)
    handler = InputHandler(config)
    handler.submit("a")
    handler.submit("b")
    drained = handler.drain_queue()
    assert len(drained) == 2
    assert drained[0].message == "a"
    assert drained[1].message == "b"
    assert handler.queue_depth() == 0


def test_input_handler_clear():
    """clear empties queue and interrupt state."""
    config = InputHandlingConfig(mode="queue", queue_limit=5)
    handler = InputHandler(config)
    handler.submit("x")
    handler.clear()
    assert handler.queue_depth() == 0
    assert handler.has_interrupt() is False


def test_input_handler_peek_interrupt():
    """peek_interrupt does not consume."""
    config = InputHandlingConfig(mode="interrupt", interrupt_behavior="cancel")
    handler = InputHandler(config)
    handler.request_interrupt("peek me")
    assert handler.peek_interrupt() is not None
    assert handler.peek_interrupt().message == "peek me"
    assert handler.has_interrupt() is True
    ui = handler.get_interrupt()
    assert ui.message == "peek me"
    assert handler.peek_interrupt() is None
