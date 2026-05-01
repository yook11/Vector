"""Embedder のファクトリ。"""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder


def get_embedder() -> BaseEmbedder:
    """Gemini embedding API 向けの GeminiEmbedder を返すファクトリ。"""
    from app.analysis.embedder.gemini import GeminiEmbedder

    return GeminiEmbedder()
