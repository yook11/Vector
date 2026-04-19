"""Embedder のファクトリ。"""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder
from app.config import settings


def get_embedder() -> BaseEmbedder:
    """TEI ローカルサーバー向けの RuriEmbedder を返すファクトリ。"""
    from app.analysis.embedder.ruri import RuriEmbedder

    return RuriEmbedder(base_url=settings.embedding_base_url)
