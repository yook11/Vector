"""Embedding module — AI embedder abstraction, providers, and orchestration."""

from app.ai.embedding.base import BaseEmbedder
from app.ai.embedding.errors import (
    DailyQuotaExhaustedError,
    EmbeddingError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.ai.embedding.factory import get_embedder
from app.ai.embedding.service import (
    EmbedResult,
    _build_embed_text,
    embed_articles,
    embed_search_query,
)

__all__ = [
    "BaseEmbedder",
    "DailyQuotaExhaustedError",
    "EmbedResult",
    "EmbeddingError",
    "InvalidInputError",
    "RateLimitError",
    "TransientError",
    "_build_embed_text",
    "embed_articles",
    "embed_search_query",
    "get_embedder",
]
