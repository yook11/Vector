"""Embedder factory."""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder
from app.config import settings


def get_embedder() -> BaseEmbedder:
    """Factory: return an embedder instance based on settings.ai_provider.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.embedder.gemini import GeminiEmbedder

        return GeminiEmbedder()
    raise ValueError(f"Unsupported AI provider for embeddings: {provider}")
