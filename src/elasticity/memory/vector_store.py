"""SQLite-backed vector store with tiered memory for the cognitive context strategy.

Embeddings are stored as raw bytes (numpy ``float32`` arrays). Cosine similarity
is computed at query time using numpy — fast enough for conversational scale
(hundreds to low thousands of entries).
"""

import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from pathlib import Path
from typing import List, Optional


class MemoryTier(str, Enum):
    SHORT_TERM = "short_term"       # individual turns from current session
    MEDIUM_TERM = "medium_term"     # topic summaries from current session
    LONG_TERM = "long_term"         # cross-session persistent memories


@dataclass
class MemoryEntry:
    """A single entry retrieved from the vector store."""

    key: str
    value: str
    tier: MemoryTier
    session_id: Optional[str] = None
    created_at: str = ""
    score: float = 0.0


def _embed_to_bytes(embedding: List[float]) -> bytes:
    """Serialize a float list to compact bytes."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _bytes_to_embed(data: bytes) -> List[float]:
    """Deserialize bytes back to a float list."""
    count = len(data) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{count}f", data))


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors using pure Python.

    Falls back gracefully when numpy is unavailable. When numpy *is* present
    the caller can batch via ``_cosine_similarity_batch`` instead.
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _cosine_similarity_batch(query: List[float], candidates: List[List[float]]) -> List[float]:
    """Vectorised cosine similarity using numpy when available."""
    try:
        import numpy as np

        q = np.array(query, dtype=np.float32)
        mat = np.array(candidates, dtype=np.float32)
        dots = mat @ q
        norms = np.linalg.norm(mat, axis=1) * np.linalg.norm(q)
        norms[norms == 0] = 1.0  # avoid division by zero
        return (dots / norms).tolist()
    except ImportError:
        return [_cosine_similarity(query, c) for c in candidates]


class VectorStore:
    """Tiered vector store backed by SQLite."""

    def __init__(self, db_path: str):
        resolved = str(Path(db_path).expanduser().resolve())
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(resolved)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cognitive_memories (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                embedding BLOB NOT NULL,
                tier TEXT NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cm_tier ON cognitive_memories(tier)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cm_session ON cognitive_memories(session_id)"
        )
        self._conn.commit()

    def store(
        self,
        key: str,
        value: str,
        embedding: List[float],
        session_id: Optional[str] = None,
        tier: str = "short_term",
    ) -> None:
        """Insert or update a memory entry."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO cognitive_memories (key, value, embedding, tier, session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                embedding = excluded.embedding,
                tier = excluded.tier,
                updated_at = excluded.updated_at
            """,
            (key, value, _embed_to_bytes(embedding), tier, session_id, now, now),
        )
        self._conn.commit()

    def search(
        self,
        query_embedding: List[float],
        limit: int = 5,
        session_id: Optional[str] = None,
        tiers: Optional[List[str]] = None,
        threshold: float = 0.0,
    ) -> List[MemoryEntry]:
        """Find the most similar memories by cosine similarity.

        Args:
            query_embedding: The query vector.
            limit: Maximum number of results.
            session_id: If set, restrict to this session only.
            tiers: If set, restrict to these tiers.
            threshold: Minimum similarity score to include.

        Returns:
            List of :class:`MemoryEntry` sorted by descending similarity.
        """
        clauses = []
        params: list = []

        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            clauses.append(f"tier IN ({placeholders})")
            params.extend(tiers)

        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT key, value, embedding, tier, session_id, created_at FROM cognitive_memories{where}",
            params,
        ).fetchall()

        if not rows:
            return []

        # Vectorised similarity
        keys = []
        values = []
        embeddings = []
        tiers_list = []
        session_ids = []
        created_ats = []

        for key, value, emb_bytes, tier, sid, created_at in rows:
            keys.append(key)
            values.append(value)
            embeddings.append(_bytes_to_embed(emb_bytes))
            tiers_list.append(tier)
            session_ids.append(sid)
            created_ats.append(created_at)

        scores = _cosine_similarity_batch(query_embedding, embeddings)

        results = []
        for i, score in enumerate(scores):
            if score >= threshold:
                results.append(
                    MemoryEntry(
                        key=keys[i],
                        value=values[i],
                        tier=MemoryTier(tiers_list[i]),
                        session_id=session_ids[i],
                        created_at=created_ats[i],
                        score=score,
                    )
                )

        results.sort(key=lambda m: m.score, reverse=True)
        return results[:limit]

    def max_session_turn_number(self, session_id: str) -> int:
        """Return the highest turn:<session_id>:<n> suffix stored for a session.

        Used by CognitiveStrategy to recover _turn_count after a process
        restart. Scans across all tiers because turns get promoted from
        short_term to long_term via active consolidation, and a tier-filtered
        count would understate the true maximum, causing key collisions on
        resumed sessions.
        """
        prefix = f"turn:{session_id}:"
        row = self._conn.execute(
            """
            SELECT MAX(CAST(substr(key, ?) AS INTEGER))
            FROM cognitive_memories
            WHERE key LIKE ?
            """,
            (len(prefix) + 1, f"{prefix}%"),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_entries_by_tier(self, session_id: str, tier: str) -> List[MemoryEntry]:
        """Return all entries for a given session and tier.

        Does not deserialize embeddings — suitable for promotion or metadata queries.
        """
        rows = self._conn.execute(
            "SELECT key, value, tier, session_id, created_at "
            "FROM cognitive_memories WHERE session_id = ? AND tier = ?",
            (session_id, tier),
        ).fetchall()
        return [
            MemoryEntry(
                key=row[0],
                value=row[1],
                tier=MemoryTier(row[2]),
                session_id=row[3],
                created_at=row[4],
            )
            for row in rows
        ]

    def promote(self, key: str, to_tier: str) -> bool:
        """Move a memory to a different tier.

        Returns True if the key existed and was updated.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE cognitive_memories SET tier = ?, updated_at = ? WHERE key = ?",
            (to_tier, now, key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def prune(
        self,
        session_id: Optional[str] = None,
        tier: Optional[str] = None,
        max_age_hours: Optional[float] = None,
    ) -> int:
        """Remove entries matching the given filters.

        Returns the number of rows deleted.
        """
        clauses = []
        params: list = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if max_age_hours is not None:
            cutoff = datetime.now(UTC).isoformat()
            # Simple approach: compare ISO strings (works for UTC)
            clauses.append("created_at < ?")
            params.append(cutoff)

        if not clauses:
            return 0

        where = " WHERE " + " AND ".join(clauses)
        cursor = self._conn.execute(
            f"DELETE FROM cognitive_memories{where}", params
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
