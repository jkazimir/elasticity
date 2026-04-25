"""Tests for the sessions REST API — US-003 (Delete Individual Chat Sessions).

Covers:
  - DELETE /api/sessions/{session_id}  →  204 / 200 on success (AC-1, AC-2, AC-3)
  - DELETE /api/sessions/{session_id}  →  404 when not found  (AC-6)
  - GET  /api/sessions                 →  session absent after deletion  (AC-3)
  - DELETE active / non-active session both return success   (AC-4 backend)
  - Cascading removal of turns and context                   (storage contract)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from datetime import datetime, UTC

import pytest
from fastapi.testclient import TestClient

from elasticity.web.server import create_app
from elasticity.storage import SessionStore, TurnRecord
from elasticity.runtime.session import Session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_config_dir(tmp_path: Path) -> Path:
    """A temporary directory that stands in for the config directory."""
    return tmp_path / "configs"


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Path to a fresh, isolated SQLite database."""
    return tmp_path / "test_sessions.db"


@pytest.fixture()
def store(tmp_db: Path) -> SessionStore:
    """A SessionStore backed by the temporary database."""
    return SessionStore(db_path=tmp_db)


@pytest.fixture()
def client(tmp_config_dir: Path, tmp_db: Path, monkeypatch) -> TestClient:
    """A TestClient wired to an app that uses the temporary database."""
    tmp_config_dir.mkdir(parents=True, exist_ok=True)

    # Point the SessionStore at our temp db for the duration of each test.
    monkeypatch.setenv("ELASTICITY_SESSION_DB", str(tmp_db))

    app = create_app(tmp_config_dir)
    return TestClient(app)


def _seed_session(
    store: SessionStore,
    session_id: str = "test-session-001",
    orchestration: str = "my_orch",
    config_path: str = "/tmp/test.yaml",
    turns: int = 2,
) -> str:
    """Insert a session (with optional turns) into the store. Returns the id."""
    s = Session(id=session_id)
    store.save_session(s, orchestration=orchestration, config_path=config_path)

    for i in range(1, turns + 1):
        store.save_turn(
            session_id,
            TurnRecord(
                turn_number=i,
                user_input=f"question {i}",
                response=f"answer {i}",
                agent_outputs={},
                token_count=10,
                duration_ms=50.0,
                created_at=datetime.now(UTC).isoformat(),
            ),
        )
    return session_id


# ---------------------------------------------------------------------------
# DELETE /api/sessions/{session_id}
# ---------------------------------------------------------------------------


class TestDeleteSession:
    """AC-1 through AC-3, AC-6: delete endpoint behaviour."""

    def test_delete_existing_session_returns_ok(self, client, store):
        """Deleting a known session returns HTTP 200 with ok=True."""
        sid = _seed_session(store, "del-ok-001")

        resp = client.delete(f"/api/sessions/{sid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["deleted"] == sid

    def test_delete_removes_session_from_list(self, client, store):
        """After deletion the session no longer appears in GET /api/sessions. (AC-3)"""
        sid = _seed_session(store, "del-list-001")

        # Confirm it exists first.
        list_resp = client.get("/api/sessions")
        ids = [s["id"] for s in list_resp.json()]
        assert sid in ids

        client.delete(f"/api/sessions/{sid}")

        list_resp_after = client.get("/api/sessions")
        ids_after = [s["id"] for s in list_resp_after.json()]
        assert sid not in ids_after

    def test_delete_nonexistent_session_returns_404(self, client):
        """Deleting a session that doesn't exist returns HTTP 404. (AC-6)"""
        resp = client.delete("/api/sessions/ghost-session-xyz")

        assert resp.status_code == 404
        body = resp.json()
        assert "not found" in body["detail"].lower()

    def test_delete_active_session_returns_ok(self, client, store):
        """Deleting the 'currently active' session (from backend's perspective) succeeds.
        The frontend is responsible for AC-4 (state reset); the API just deletes. (AC-4)
        """
        sid = _seed_session(store, "active-session-001", turns=3)

        resp = client.delete(f"/api/sessions/{sid}")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_cascades_turns_and_context(self, client, store):
        """Deleting a session removes its turns and context from the database. (AC-6 / storage)"""
        sid = _seed_session(store, "cascade-api-001", turns=2)
        store.save_context(sid, {"some_key": "some_value"})

        client.delete(f"/api/sessions/{sid}")

        # The session and its children are gone.
        assert store.load_session(sid) is None

    def test_delete_second_request_returns_404(self, client, store):
        """Deleting the same session twice: second call is a 404. (AC-6 idempotency)"""
        sid = _seed_session(store, "double-del-001")

        first  = client.delete(f"/api/sessions/{sid}")
        second = client.delete(f"/api/sessions/{sid}")

        assert first.status_code == 200
        assert second.status_code == 404

    def test_delete_does_not_affect_other_sessions(self, client, store):
        """Only the targeted session is removed; siblings are untouched. (AC-3)"""
        sid_a = _seed_session(store, "keep-me-001")
        sid_b = _seed_session(store, "delete-me-001")

        client.delete(f"/api/sessions/{sid_b}")

        list_resp = client.get("/api/sessions")
        remaining_ids = [s["id"] for s in list_resp.json()]
        assert sid_a in remaining_ids
        assert sid_b not in remaining_ids

    def test_delete_session_with_no_turns(self, client, store):
        """Sessions with zero turns can also be deleted cleanly."""
        sid = _seed_session(store, "empty-session-001", turns=0)

        resp = client.delete(f"/api/sessions/{sid}")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# GET /api/sessions (list) — sanity check that list still works post-delete
# ---------------------------------------------------------------------------


class TestListSessionsAfterDelete:
    """Verify the list endpoint is unaffected by deletions of other sessions."""

    def test_list_returns_remaining_sessions(self, client, store):
        """After deleting one of three sessions, the list returns exactly two."""
        ids = [_seed_session(store, f"list-check-{i:03d}") for i in range(3)]

        client.delete(f"/api/sessions/{ids[1]}")

        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        returned_ids = [s["id"] for s in resp.json()]
        assert ids[0] in returned_ids
        assert ids[1] not in returned_ids
        assert ids[2] in returned_ids

    def test_list_empty_after_all_deleted(self, client, store):
        """Deleting all sessions results in an empty list — not an error."""
        sid = _seed_session(store, "lone-session-001")

        client.delete(f"/api/sessions/{sid}")

        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/sessions?config_id=… — config_id filtering (including nested ~)
# ---------------------------------------------------------------------------

MINIMAL_YAML = "agent_types: {}\ntools: {}\norchestrations: {}\n"


class TestListSessionsConfigFilter:
    """Verify that config_id filtering works for nested (tilde-separated) configs."""

    def test_nested_config_id_filters_correctly(self, client, store, tmp_config_dir):
        """Only sessions for the requested nested config should be returned."""
        # Create two nested config files.
        subdir = tmp_config_dir / "teams"
        subdir.mkdir()
        alpha = subdir / "alpha.yaml"
        beta = subdir / "beta.yaml"
        alpha.write_text(MINIMAL_YAML)
        beta.write_text(MINIMAL_YAML)

        # Seed sessions with their resolved config paths.
        _seed_session(store, "sess-alpha", config_path=str(alpha.resolve()))
        _seed_session(store, "sess-beta", config_path=str(beta.resolve()))

        # Request with the tilde-separated config_id for alpha.
        resp = client.get("/api/sessions?config_id=teams~alpha")
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()]
        assert "sess-alpha" in ids
        assert "sess-beta" not in ids

    def test_nonexistent_config_returns_empty(self, client, store, tmp_config_dir):
        """A config_id that resolves to a missing file returns no sessions."""
        _seed_session(store, "sess-orphan", config_path="/tmp/does-not-matter.yaml")

        resp = client.get("/api/sessions?config_id=teams~nonexistent")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_no_filter_returns_all(self, client, store):
        """Omitting config_id returns all sessions regardless of config_path."""
        _seed_session(store, "sess-a", config_path="/configs/a.yaml")
        _seed_session(store, "sess-b", config_path="/configs/b.yaml")

        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()]
        assert "sess-a" in ids
        assert "sess-b" in ids

    def test_path_traversal_rejected(self, client):
        """Tilde-separated paths containing '..' should be rejected."""
        resp = client.get("/api/sessions?config_id=..~etc~passwd")
        assert resp.status_code == 400
