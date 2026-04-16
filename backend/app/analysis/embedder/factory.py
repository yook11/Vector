"""Embedder のファクトリ。"""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder
from app.config import settings


def get_embedder() -> BaseEmbedder:
    """``settings.ai_provider`` に応じた embedder インスタンスを返すファクトリ。

    Raises:
        ValueError: サポートされていない ai_provider が指定された場合。
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.embedder.gemini import GeminiEmbedder

        return GeminiEmbedder()
    raise ValueError(f"Unsupported AI provider for embeddings: {provider}")
