"""Embedding providers for the cognitive context strategy.

Supports multiple backends via the ``provider/model`` pattern already used
for LLM backends (e.g. ``local/all-MiniLM-L6-v2``, ``voyage/voyage-3-lite``,
``openai/text-embedding-3-small``).
"""

import logging
from abc import ABC, abstractmethod
from typing import List, Optional

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Protocol for producing text embeddings."""

    @abstractmethod
    async def embed(self, text: str) -> List[float]:
        """Return an embedding vector for *text*."""

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Return embeddings for multiple texts.  Default: sequential calls."""
        return [await self.embed(t) for t in texts]


class LocalEmbeddingProvider(EmbeddingProvider):
    """Offline embeddings via ``sentence-transformers``.

    Requires the ``cognitive`` extras (``pip install elasticity[cognitive]``).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers is required for the local embedding provider. "
                "Install it with: pip install elasticity[cognitive]"
            )
        self._model = SentenceTransformer(model_name)

    async def embed(self, text: str) -> List[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        embeddings = self._model.encode(texts, normalize_embeddings=True)
        return [e.tolist() for e in embeddings]


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Embeddings via the Voyage AI API (Anthropic's recommended partner).

    Requires the ``voyageai`` package (``pip install elasticity[voyage]``).
    """

    def __init__(self, model_name: str = "voyage-3-lite", api_key: Optional[str] = None):
        try:
            import voyageai  # noqa: F401
        except ImportError:
            raise ImportError(
                "voyageai is required for the Voyage embedding provider. "
                "Install it with: pip install elasticity[voyage]"
            )
        import os

        self._model = model_name
        key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise ValueError(
                "Voyage API key required. Set VOYAGE_API_KEY or pass api_key."
            )
        self._client = voyageai.Client(api_key=key)

    async def embed(self, text: str) -> List[float]:
        result = self._client.embed([text], model=self._model)
        return result.embeddings[0]

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        result = self._client.embed(texts, model=self._model)
        return result.embeddings


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embeddings via any OpenAI-compatible endpoint.

    Uses the ``openai`` SDK already required by the framework.
    """

    def __init__(self, model_name: str = "text-embedding-3-small", api_key: Optional[str] = None, base_url: Optional[str] = None):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai is required for the OpenAI embedding provider. "
                "Install it with: pip install openai"
            )
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._model = model_name

    async def embed(self, text: str) -> List[float]:
        resp = self._client.embeddings.create(input=[text], model=self._model)
        return resp.data[0].embedding

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        resp = self._client.embeddings.create(input=texts, model=self._model)
        return [d.embedding for d in resp.data]


def resolve_embedding_provider(
    provider_model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> EmbeddingProvider:
    """Construct an :class:`EmbeddingProvider` from a ``provider/model`` string.

    Examples::

        resolve_embedding_provider("local/all-MiniLM-L6-v2")
        resolve_embedding_provider("voyage/voyage-3-lite")
        resolve_embedding_provider("openai/text-embedding-3-small")
    """
    if "/" not in provider_model:
        raise ValueError(
            f"embedding_provider must be 'provider/model', got '{provider_model}'"
        )
    provider, model = provider_model.split("/", 1)
    if provider == "local":
        return LocalEmbeddingProvider(model_name=model)
    elif provider == "voyage":
        return VoyageEmbeddingProvider(model_name=model, api_key=api_key)
    elif provider == "openai":
        return OpenAIEmbeddingProvider(model_name=model, api_key=api_key, base_url=base_url)
    else:
        raise ValueError(
            f"Unknown embedding provider '{provider}'. "
            f"Supported: local, voyage, openai"
        )
