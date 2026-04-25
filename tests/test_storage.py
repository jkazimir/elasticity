"""Tests for SQLite session persistence."""

import tempfile
from datetime import datetime, UTC
from pathlib import Path

import pytest

from elasticity.runtime.session import Session
from elasticity.storage import SessionStore, TurnRecord, SessionSummary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """A fresh SessionStore backed by a temp file."""
    return SessionStore(db_path=tmp_path / "sessions.db")


def _make_session(session_id: str = "s1") -> Session:
    s = Session(id=session_id)
    return s


def _make_turn(n: int = 1, user: str = "hello", response: str = "hi") -> TurnRecord:
    return TurnRecord(
        turn_number=n,
        user_input=user,
        response=response,
        agent_outputs={},
        token_count=0,
        duration_ms=0.0,
        created_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


def test_save_and_load_session(store):
    session = _make_session("abc")
    store.save_session(session, orchestration="test_orch", config_path="/tmp/config.yaml")

    loaded = store.load_session("abc")
    assert loaded is not None
    assert loaded.id == "abc"


def test_load_nonexistent_returns_none(store):
    assert store.load_session("nonexistent") is None


def test_session_exists(store):
    session = _make_session("xyz")
    assert not store.session_exists("xyz")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")
    assert store.session_exists("xyz")


def test_delete_session(store):
    session = _make_session("del1")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")
    assert store.session_exists("del1")

    result = store.delete_session("del1")
    assert result is True
    assert not store.session_exists("del1")


def test_delete_nonexistent_returns_false(store):
    assert store.delete_session("ghost") is False


# ---------------------------------------------------------------------------
# Turn persistence
# ---------------------------------------------------------------------------


def test_save_turn_restores_message_history(store):
    session = _make_session("t1")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")

    store.save_turn("t1", _make_turn(1, "Hello", "Hi there"))
    store.save_turn("t1", _make_turn(2, "How are you?", "I'm good"))

    loaded = store.load_session("t1")
    assert loaded is not None
    assert len(loaded.message_history) == 4  # 2 turns × 2 messages each
    assert loaded.message_history[0] == {"role": "user", "content": "Hello"}
    assert loaded.message_history[1] == {"role": "assistant", "content": "Hi there"}
    assert loaded.message_history[2] == {"role": "user", "content": "How are you?"}


def test_session_updated_at_bumps_on_turn(store):
    session = _make_session("bump")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")

    summaries_before = store.list_sessions()
    ts_before = summaries_before[0].updated_at

    import time
    time.sleep(0.01)  # ensure timestamp advances
    store.save_turn("bump", _make_turn(1, "q", "a"))

    summaries_after = store.list_sessions()
    ts_after = summaries_after[0].updated_at
    assert ts_after >= ts_before


# ---------------------------------------------------------------------------
# Context persistence
# ---------------------------------------------------------------------------


def test_save_and_restore_context(store):
    session = _make_session("ctx1")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")

    store.save_context("ctx1", {"key1": "value1", "score": 0.95, "flag": True})

    loaded = store.load_session("ctx1")
    assert loaded is not None
    assert loaded.context["key1"] == "value1"
    assert loaded.context["score"] == pytest.approx(0.95)
    assert loaded.context["flag"] is True


def test_save_context_overwrites_existing(store):
    session = _make_session("ctx2")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")

    store.save_context("ctx2", {"key": "first"})
    store.save_context("ctx2", {"key": "second"})

    loaded = store.load_session("ctx2")
    assert loaded.context["key"] == "second"


# ---------------------------------------------------------------------------
# Listing sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_all(store):
    for i in range(3):
        s = _make_session(f"s{i}")
        store.save_session(s, orchestration="orch", config_path="/tmp/c.yaml")

    summaries = store.list_sessions()
    assert len(summaries) == 3
    assert all(isinstance(s, SessionSummary) for s in summaries)


def test_list_sessions_filtered_by_config(store):
    s1 = _make_session("f1")
    s2 = _make_session("f2")
    store.save_session(s1, orchestration="orch", config_path="/tmp/a.yaml")
    store.save_session(s2, orchestration="orch", config_path="/tmp/b.yaml")

    results = store.list_sessions(config_path="/tmp/a.yaml")
    assert len(results) == 1
    assert results[0].id == "f1"


def test_list_sessions_ordered_by_updated_at_desc(store):
    import time

    for sid in ["first", "second", "third"]:
        s = _make_session(sid)
        store.save_session(s, orchestration="orch", config_path="/tmp/c.yaml")
        time.sleep(0.01)

    summaries = store.list_sessions()
    assert summaries[0].id == "third"
    assert summaries[-1].id == "first"


def test_list_sessions_includes_turn_count(store):
    s = _make_session("tc1")
    store.save_session(s, orchestration="orch", config_path="/tmp/c.yaml")
    store.save_turn("tc1", _make_turn(1, "a", "b"))
    store.save_turn("tc1", _make_turn(2, "c", "d"))

    summaries = store.list_sessions()
    assert summaries[0].turn_count == 2


# ---------------------------------------------------------------------------
# get_latest_session
# ---------------------------------------------------------------------------


def test_get_latest_session(store):
    import time

    for i in range(3):
        s = _make_session(f"latest_{i}")
        store.save_session(s, orchestration="orch", config_path="/tmp/c.yaml")
        store.save_turn(f"latest_{i}", _make_turn(1, f"q{i}", f"a{i}"))
        time.sleep(0.01)

    latest = store.get_latest_session("/tmp/c.yaml", "orch")
    assert latest is not None
    assert latest.id == "latest_2"


def test_get_latest_session_returns_none_when_empty(store):
    result = store.get_latest_session("/tmp/missing.yaml", "orch")
    assert result is None


# ---------------------------------------------------------------------------
# Cascading delete
# ---------------------------------------------------------------------------


def test_save_and_load_pending_queue(store):
    """Pending queue is persisted and restored."""
    session = _make_session("pq1")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")
    store.save_pending_queue("pq1", ["msg1", "msg2"])
    loaded = store.load_pending_queue("pq1")
    assert loaded == ["msg1", "msg2"]


def test_load_pending_queue_empty(store):
    """Load pending queue returns empty when none."""
    assert store.load_pending_queue("nonexistent") == []


def test_clear_pending_queue(store):
    """clear_pending_queue removes persisted queue."""
    session = _make_session("pq2")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")
    store.save_pending_queue("pq2", ["x"])
    store.clear_pending_queue("pq2")
    assert store.load_pending_queue("pq2") == []


def test_delete_session_removes_turns_and_context(store):
    session = _make_session("cascade")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")
    store.save_turn("cascade", _make_turn(1, "hi", "hello"))
    store.save_context("cascade", {"k": "v"})

    store.delete_session("cascade")
    loaded = store.load_session("cascade")
    assert loaded is None


def test_save_pending_queue_preserves_other_context(store):
    """save_pending_queue should not wipe sibling context keys."""
    session = _make_session("pq-ctx")
    store.save_session(session, orchestration="orch", config_path="/tmp/c.yaml")

    # Save some context first
    store.save_context("pq-ctx", {"user_key": "important", "score": 42})

    # Now save a pending queue — this should NOT destroy user_key/score
    store.save_pending_queue("pq-ctx", ["queued-msg"])

    loaded = store.load_session("pq-ctx")
    assert loaded is not None
    assert loaded.context["user_key"] == "important"
    assert loaded.context["score"] == 42
    assert loaded.context["_pending_queue"] == ["queued-msg"]
