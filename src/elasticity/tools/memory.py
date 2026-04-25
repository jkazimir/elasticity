"""Memory tools for the hippocampus agent - store and retrieve key-value memories."""

import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, Optional

# In-memory store for fallback when SQLite is not initialized
_memory_store: Dict[str, str] = {}

# Config-keyed connection pool — supports multiple databases in the same process
_connections: Dict[str, sqlite3.Connection] = {}
_default_db_path: Optional[str] = None


def _resolve_db_path(config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve the database path from config, or return the default."""
    if config and "db_path" in config:
        return str(Path(config["db_path"]).expanduser().resolve())
    return _default_db_path


def _get_conn(config: Optional[Dict[str, Any]] = None) -> Optional[sqlite3.Connection]:
    """Get the connection for a given config, falling back to the default."""
    db_path = _resolve_db_path(config)
    if db_path:
        return _connections.get(db_path)
    return None


def _tool_init(config: Dict[str, Any]) -> None:
    """Initialize the memory tool with SQLite persistence.

    Args:
        config: Tool configuration dict, expected to contain 'db_path' key
    """
    global _default_db_path

    db_path = config.get("db_path", "./memory.db")
    resolved = str(Path(db_path).expanduser().resolve())

    # Ensure parent directory exists
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)

    _default_db_path = resolved

    if resolved not in _connections:
        conn = sqlite3.connect(resolved)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        _connections[resolved] = conn


def store(key: str, value: str, _tool_config: Optional[Dict[str, Any]] = None) -> str:
    """Store a key-value pair in the memory store.

    Args:
        key: Memory key/identifier
        value: Content to store
        _tool_config: Injected by the framework — selects the correct database

    Returns:
        Confirmation message
    """
    conn = _get_conn(_tool_config)
    if conn is not None:
        now = datetime.now(UTC).isoformat()
        conn.execute("""
            INSERT INTO memories (key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (key, value, now, now))
        conn.commit()
        return f"Stored memory under key '{key}'"
    else:
        _memory_store[key] = value
        return f"Stored memory under key '{key}'"


def get_by_key(key: str, _tool_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Retrieve a memory value by exact key.

    Args:
        key: The exact memory key to look up.
        _tool_config: Injected by the framework — selects the correct database

    Returns:
        The stored value, or None if the key does not exist.
    """
    conn = _get_conn(_tool_config)
    if conn is not None:
        cursor = conn.execute(
            "SELECT value FROM memories WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        return row[0] if row else None
    else:
        return _memory_store.get(key)


def retrieve(query: str, _tool_config: Optional[Dict[str, Any]] = None) -> str:
    """Retrieve memories by semantic query.

    For this implementation, returns memories whose keys or values match the query.
    A production implementation could use vector similarity search.

    Args:
        query: Semantic query to search for relevant memories
        _tool_config: Injected by the framework — selects the correct database

    Returns:
        Retrieved memories as formatted string
    """
    conn = _get_conn(_tool_config)
    if conn is not None:
        query_lower = query.lower()
        cursor = conn.execute("""
            SELECT key, value, created_at, updated_at
            FROM memories
            WHERE LOWER(key) LIKE ? OR LOWER(value) LIKE ?
        """, (f"%{query_lower}%", f"%{query_lower}%"))
        rows = cursor.fetchall()

        if not rows:
            # Fallback: return all memories
            cursor = conn.execute("SELECT key, value, created_at, updated_at FROM memories")
            rows = cursor.fetchall()

        if not rows:
            return "No memories found."

        # Format results with timestamps
        results = []
        for key, value, created_at, updated_at in rows:
            results.append(f"[{key}]: {value}\n  Created: {created_at}\n  Updated: {updated_at}")
        return "\n---\n".join(results)
    else:
        # Fallback to in-memory dict
        if not _memory_store:
            return "No memories found."

        query_lower = query.lower()
        matches = {
            k: v for k, v in _memory_store.items()
            if query_lower in k.lower() or query_lower in v.lower()
        }
        if not matches:
            matches = _memory_store  # Fallback: return all

        return "\n---\n".join(f"[{k}]: {v}" for k, v in matches.items())
