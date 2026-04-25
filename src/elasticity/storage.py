"""SQLite-backed session persistence for conversational orchestrations.

Sessions are stored in the XDG data directory
(``~/.local/share/elasticity/sessions.db``) by default.

Override resolution order (highest priority first):
1. ``ELASTICITY_SESSION_DB`` environment variable
2. ``storage.session_db`` in ``~/.config/elasticity/config.yaml``
3. ``ELASTICITY_DATA_DIR`` / ``XDG_DATA_HOME`` / ``~/.local/share/elasticity/``
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runtime.session import Session
from .config.global_loader import get_data_dir, load_global_config


def _get_db_path() -> Path:
    env = os.environ.get("ELASTICITY_SESSION_DB")
    if env:
        return Path(env).expanduser()

    global_cfg = load_global_config()
    if global_cfg.storage.session_db:
        return Path(global_cfg.storage.session_db).expanduser()

    return get_data_dir() / "sessions.db"


@dataclass
class TurnRecord:
    """A single conversation turn stored in the database."""

    turn_number: int
    user_input: str
    response: str
    agent_outputs: Dict[str, Any]
    token_count: int
    duration_ms: float
    created_at: str
    status: str = "complete"  # "pending", "complete", "error"
    id: Optional[int] = None  # Set after save; used to complete a pending turn


@dataclass
class SessionSummary:
    """Lightweight summary for listing sessions."""

    id: str
    orchestration: str
    config_path: str
    title: str
    turn_count: int
    created_at: str
    updated_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    orchestration TEXT NOT NULL,
    config_path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,
    user_input TEXT NOT NULL,
    response TEXT NOT NULL DEFAULT '',
    agent_outputs TEXT NOT NULL DEFAULT '{}',
    token_count INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'complete'
);

CREATE TABLE IF NOT EXISTS context (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);
"""


class SessionStore:
    """Persistent storage for conversation sessions using SQLite."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _get_db_path()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Migration: add status column for existing databases
            try:
                conn.execute(
                    "ALTER TABLE turns ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'"
                )
            except Exception:
                pass  # Column already exists

    # ------------------------------------------------------------------
    # Save / update
    # ------------------------------------------------------------------

    def save_session(
        self,
        session: Session,
        orchestration: str,
        config_path: str,
        title: str = "",
    ) -> None:
        """Insert or update a session record."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, orchestration, config_path, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, title=excluded.title
                """,
                (session.id, orchestration, config_path, title or orchestration, now, now),
            )

    def save_turn(self, session_id: str, turn: TurnRecord) -> None:
        """Append a turn record and update the session's updated_at timestamp."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO turns
                    (session_id, turn_number, user_input, response,
                     agent_outputs, token_count, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn.turn_number,
                    turn.user_input,
                    turn.response,
                    json.dumps(turn.agent_outputs),
                    turn.token_count,
                    turn.duration_ms,
                    turn.created_at,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )

    def save_pending_turn(
        self,
        session_id: str,
        turn_number: int,
        user_input: str,
        created_at: str,
    ) -> int:
        """Insert a turn with status='pending' before agent execution starts.

        Returns the row id so the turn can be completed via ``complete_turn()``.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO turns
                    (session_id, turn_number, user_input, response,
                     agent_outputs, token_count, duration_ms, created_at, status)
                VALUES (?, ?, ?, '', '{}', 0, 0, ?, 'pending')
                """,
                (session_id, turn_number, user_input, created_at),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (created_at, session_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def complete_turn(
        self,
        turn_id: int,
        response: str,
        agent_outputs: Dict[str, Any],
        duration_ms: float,
        status: str = "complete",
    ) -> None:
        """Update a pending turn with the completed response and event data."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE turns
                SET response=?, agent_outputs=?, duration_ms=?, status=?
                WHERE id=?
                """,
                (
                    response,
                    json.dumps(agent_outputs),
                    duration_ms,
                    status,
                    turn_id,
                ),
            )
            # Update session timestamp
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?)",
                (now, turn_id),
            )

    def get_turns(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all turn rows for a session, ordered by turn_number.

        Each dict includes parsed ``agent_outputs`` and all other columns.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, turn_number, user_input, response,
                       agent_outputs, token_count, duration_ms, created_at, status
                FROM turns
                WHERE session_id=?
                ORDER BY turn_number ASC
                """,
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                outputs = json.loads(row["agent_outputs"])
            except (json.JSONDecodeError, TypeError):
                outputs = {}
            result.append(
                {
                    "id": row["id"],
                    "turn_number": row["turn_number"],
                    "user_input": row["user_input"],
                    "response": row["response"],
                    "agent_outputs": outputs,
                    "token_count": row["token_count"],
                    "duration_ms": row["duration_ms"],
                    "created_at": row["created_at"],
                    "status": row["status"],
                }
            )
        return result

    def save_context(self, session_id: str, context: Dict[str, Any]) -> None:
        """Persist the session's shared context key-value pairs.

        Keys present in *context* are upserted. Keys previously persisted but
        no longer present in *context* are deleted so the DB stays in sync.
        """
        with self._connect() as conn:
            # Upsert current keys
            for key, value in context.items():
                conn.execute(
                    """
                    INSERT INTO context (session_id, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value
                    """,
                    (session_id, key, json.dumps(value, default=str)),
                )
            # Delete keys that are no longer present
            if context:
                placeholders = ",".join("?" * len(context))
                conn.execute(
                    f"DELETE FROM context WHERE session_id=? AND key NOT IN ({placeholders})",
                    (session_id, *context.keys()),
                )
            else:
                conn.execute(
                    "DELETE FROM context WHERE session_id=?",
                    (session_id,),
                )

    def save_pending_queue(
        self, session_id: str, messages: List[str]
    ) -> None:
        """Persist queued messages for session resume after interrupt/shutdown."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO context (session_id, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value
                """,
                (session_id, "_pending_queue", json.dumps(messages)),
            )

    def load_pending_queue(self, session_id: str) -> List[str]:
        """Load queued messages for session resume. Returns empty list if none."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM context WHERE session_id=? AND key=?",
                (session_id, "_pending_queue"),
            ).fetchone()
            if not row:
                return []
            try:
                data = json.loads(row[0])
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

    def clear_pending_queue(self, session_id: str) -> None:
        """Clear persisted queued messages."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM context WHERE session_id=? AND key=?",
                (session_id, "_pending_queue"),
            )

    # ------------------------------------------------------------------
    # Load / query
    # ------------------------------------------------------------------

    def load_session(self, session_id: str) -> Optional[Session]:
        """Restore a Session object with its full message history and context."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                return None

            session = Session(id=session_id)
            session.created_at = datetime.fromisoformat(row["created_at"])

            # Restore message history from turns (skip pending/incomplete turns)
            turns = conn.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY turn_number ASC",
                (session_id,),
            ).fetchall()
            for turn in turns:
                turn_status = turn["status"] if "status" in turn.keys() else "complete"
                if turn_status not in ("complete",):
                    continue
                session.message_history.append(
                    {"role": "user", "content": turn["user_input"]}
                )
                session.message_history.append(
                    {"role": "assistant", "content": turn["response"]}
                )

            # Restore context
            ctx_rows = conn.execute(
                "SELECT key, value FROM context WHERE session_id=?", (session_id,)
            ).fetchall()
            for ctx_row in ctx_rows:
                try:
                    session.context[ctx_row["key"]] = json.loads(ctx_row["value"])
                except (json.JSONDecodeError, TypeError):
                    session.context[ctx_row["key"]] = ctx_row["value"]

            return session

    def get_latest_session(
        self, config_path: str, orchestration: str
    ) -> Optional[Session]:
        """Return the most recently updated session for a given config + orchestration."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM sessions
                WHERE config_path=? AND orchestration=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (config_path, orchestration),
            ).fetchone()
            if not row:
                return None
            return self.load_session(row["id"])

    def list_sessions(
        self, config_path: Optional[str] = None
    ) -> List[SessionSummary]:
        """List sessions, optionally filtered by config file path."""
        with self._connect() as conn:
            if config_path:
                rows = conn.execute(
                    """
                    SELECT s.id, s.orchestration, s.config_path, s.title,
                           s.created_at, s.updated_at,
                           COUNT(t.id) AS turn_count
                    FROM sessions s
                    LEFT JOIN turns t ON t.session_id = s.id
                    WHERE s.config_path=?
                    GROUP BY s.id
                    ORDER BY s.updated_at DESC
                    """,
                    (config_path,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT s.id, s.orchestration, s.config_path, s.title,
                           s.created_at, s.updated_at,
                           COUNT(t.id) AS turn_count
                    FROM sessions s
                    LEFT JOIN turns t ON t.session_id = s.id
                    GROUP BY s.id
                    ORDER BY s.updated_at DESC
                    """
                ).fetchall()

            return [
                SessionSummary(
                    id=row["id"],
                    orchestration=row["orchestration"],
                    config_path=row["config_path"],
                    title=row["title"],
                    turn_count=row["turn_count"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its turns and context. Returns True if found.

        Accepts either a full session ID or a unique prefix (e.g. the 8-char
        prefix shown in `elasticity sessions`).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ?", (session_id + "%",)
            ).fetchall()
            if len(rows) != 1:
                return False
            full_id = rows[0][0]
            result = conn.execute("DELETE FROM sessions WHERE id=?", (full_id,))
            return result.rowcount > 0

    def session_exists(self, session_id: str) -> bool:
        """Return True if the given session ID exists in the database."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return row is not None
