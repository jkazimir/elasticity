"""Tests for the embedding provider resolver."""

import pytest

from elasticity.memory.embeddings import resolve_embedding_provider


def test_resolve_invalid_format():
    with pytest.raises(ValueError, match="provider/model"):
        resolve_embedding_provider("no-slash-here")


def test_resolve_unknown_provider():
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        resolve_embedding_provider("unknown/model")


def test_resolve_local_missing_dependency():
    """LocalEmbeddingProvider should raise ImportError if sentence-transformers not installed."""
    # This test passes if sentence-transformers IS installed (provider constructed ok)
    # or if it ISN'T (ImportError raised). Either way, resolve_embedding_provider
    # should not crash with an unexpected error.
    try:
        provider = resolve_embedding_provider("local/all-MiniLM-L6-v2")
        assert provider is not None
    except ImportError as e:
        assert "sentence-transformers" in str(e)


def test_resolve_openai_constructs():
    """OpenAIEmbeddingProvider should construct if openai is installed."""
    try:
        provider = resolve_embedding_provider(
            "openai/text-embedding-3-small", api_key="test-key"
        )
        assert provider is not None
    except ImportError:
        pytest.skip("openai not installed")


def test_resolve_voyage_requires_key():
    """VoyageEmbeddingProvider should require an API key."""
    try:
        import voyageai  # noqa: F401
    except ImportError:
        pytest.skip("voyageai not installed")

    import os
    old = os.environ.pop("VOYAGE_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API key"):
            resolve_embedding_provider("voyage/voyage-3-lite")
    finally:
        if old is not None:
            os.environ["VOYAGE_API_KEY"] = old
