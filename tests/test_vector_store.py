"""Tests for the cognitive memory vector store."""

import os
import tempfile

import pytest

from elasticity.memory.vector_store import (
    MemoryTier,
    VectorStore,
    _cosine_similarity,
    _cosine_similarity_batch,
    _embed_to_bytes,
    _bytes_to_embed,
)


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_cognitive.db")
    s = VectorStore(db_path)
    yield s
    s.close()


# ---- serialisation round-trip ----

def test_embed_roundtrip():
    vec = [0.1, 0.2, 0.3, -0.5]
    restored = _bytes_to_embed(_embed_to_bytes(vec))
    assert len(restored) == len(vec)
    for a, b in zip(vec, restored):
        assert abs(a - b) < 1e-6


# ---- cosine similarity ----

def test_cosine_similarity_identical():
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(_cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity_opposite():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6


def test_cosine_similarity_batch():
    query = [1.0, 0.0]
    candidates = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    scores = _cosine_similarity_batch(query, candidates)
    assert len(scores) == 3
    assert abs(scores[0] - 1.0) < 1e-6
    assert abs(scores[1]) < 1e-6
    assert abs(scores[2] - (-1.0)) < 1e-6


# ---- store and search ----

def test_store_and_search_basic(store):
    store.store(
        key="test:1",
        value="hello world",
        embedding=[1.0, 0.0, 0.0],
        session_id="sess1",
        tier=MemoryTier.SHORT_TERM.value,
    )
    store.store(
        key="test:2",
        value="goodbye world",
        embedding=[0.0, 1.0, 0.0],
        session_id="sess1",
        tier=MemoryTier.SHORT_TERM.value,
    )

    results = store.search(
        query_embedding=[1.0, 0.0, 0.0],
        limit=5,
        session_id="sess1",
    )
    assert len(results) >= 1
    assert results[0].key == "test:1"
    assert results[0].score > 0.9


def test_search_respects_tier_filter(store):
    store.store("a", "short", [1.0, 0.0], tier=MemoryTier.SHORT_TERM.value)
    store.store("b", "medium", [0.9, 0.1], tier=MemoryTier.MEDIUM_TERM.value)
    store.store("c", "long", [0.8, 0.2], tier=MemoryTier.LONG_TERM.value)

    results = store.search(
        query_embedding=[1.0, 0.0],
        tiers=[MemoryTier.MEDIUM_TERM.value],
    )
    assert len(results) == 1
    assert results[0].key == "b"


def test_search_respects_session_filter(store):
    store.store("a", "sess1", [1.0, 0.0], session_id="s1", tier=MemoryTier.SHORT_TERM.value)
    store.store("b", "sess2", [0.9, 0.1], session_id="s2", tier=MemoryTier.SHORT_TERM.value)
    store.store("c", "longterm", [0.8, 0.2], session_id=None, tier=MemoryTier.LONG_TERM.value)

    results = store.search(
        query_embedding=[1.0, 0.0],
        session_id="s1",
    )
    keys = {r.key for r in results}
    assert "a" in keys
    assert "c" not in keys  # long-term is session-isolated; not returned for other sessions
    assert "b" not in keys  # different session


def test_search_threshold(store):
    store.store("close", "close", [1.0, 0.0], tier=MemoryTier.SHORT_TERM.value)
    store.store("far", "far", [0.0, 1.0], tier=MemoryTier.SHORT_TERM.value)

    results = store.search(
        query_embedding=[1.0, 0.0],
        threshold=0.5,
    )
    assert len(results) == 1
    assert results[0].key == "close"


def test_search_empty_store(store):
    results = store.search(query_embedding=[1.0, 0.0])
    assert results == []


def test_search_ordering(store):
    store.store("best", "best", [1.0, 0.0, 0.0], tier=MemoryTier.SHORT_TERM.value)
    store.store("mid", "mid", [0.7, 0.7, 0.0], tier=MemoryTier.SHORT_TERM.value)
    store.store("worst", "worst", [0.0, 0.0, 1.0], tier=MemoryTier.SHORT_TERM.value)

    results = store.search(query_embedding=[1.0, 0.0, 0.0], limit=3)
    assert results[0].key == "best"
    assert results[0].score > results[1].score


# ---- get_entries_by_tier ----

def test_get_entries_by_tier(store):
    store.store("m1", "topic A", [1.0, 0.0], session_id="s1", tier=MemoryTier.MEDIUM_TERM.value)
    store.store("m2", "topic B", [0.9, 0.1], session_id="s1", tier=MemoryTier.MEDIUM_TERM.value)
    store.store("s1", "turn", [0.8, 0.2], session_id="s1", tier=MemoryTier.SHORT_TERM.value)
    store.store("m3", "other session", [0.7, 0.3], session_id="s2", tier=MemoryTier.MEDIUM_TERM.value)

    entries = store.get_entries_by_tier("s1", MemoryTier.MEDIUM_TERM.value)
    keys = {e.key for e in entries}
    assert keys == {"m1", "m2"}  # only s1's medium-term entries


def test_get_entries_by_tier_empty(store):
    entries = store.get_entries_by_tier("nonexistent", MemoryTier.MEDIUM_TERM.value)
    assert entries == []


# ---- promote ----

def test_promote(store):
    store.store("x", "val", [1.0], tier=MemoryTier.SHORT_TERM.value)
    assert store.promote("x", MemoryTier.LONG_TERM.value)

    results = store.search(
        query_embedding=[1.0],
        tiers=[MemoryTier.LONG_TERM.value],
    )
    assert len(results) == 1
    assert results[0].key == "x"


def test_promote_nonexistent(store):
    assert not store.promote("nonexistent", MemoryTier.LONG_TERM.value)


# ---- prune ----

def test_prune_by_session(store):
    store.store("a", "v", [1.0], session_id="s1", tier=MemoryTier.SHORT_TERM.value)
    store.store("b", "v", [0.5], session_id="s2", tier=MemoryTier.SHORT_TERM.value)

    deleted = store.prune(session_id="s1")
    assert deleted == 1

    results = store.search(query_embedding=[1.0])
    assert len(results) == 1
    assert results[0].key == "b"


def test_prune_by_tier(store):
    store.store("a", "v", [1.0], tier=MemoryTier.SHORT_TERM.value)
    store.store("b", "v", [0.5], tier=MemoryTier.LONG_TERM.value)

    deleted = store.prune(tier=MemoryTier.SHORT_TERM.value)
    assert deleted == 1

    results = store.search(query_embedding=[1.0])
    assert len(results) == 1
    assert results[0].key == "b"


# ---- upsert ----

def test_upsert_updates_value(store):
    store.store("k", "v1", [1.0, 0.0], tier=MemoryTier.SHORT_TERM.value)
    store.store("k", "v2", [1.0, 0.0], tier=MemoryTier.SHORT_TERM.value)

    results = store.search(query_embedding=[1.0, 0.0])
    assert len(results) == 1
    assert results[0].value == "v2"
