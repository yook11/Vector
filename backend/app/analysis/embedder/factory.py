"""Embedder のファクトリ。"""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder
from app.config import settings


def get_embedder() -> BaseEmbedder:
    """設定された provider に従って Embedder を返す。

    - "gemini" (default): production / dev (要 GEMINI_API_KEY)
    - "stub": CI / Schemathesis 等で外部 API 到達を避ける用

    production で stub が選ばれた場合は ValueError で起動時 reject する
    (CI 設定漏れが本番に滲む経路を構造的に塞ぐ)。
    """
    provider = settings.embedder_provider
    if provider == "stub":
        if settings.env == "production":
            msg = "embedder_provider='stub' is not allowed in production"
            raise ValueError(msg)
        from app.analysis.embedder.stub import StubEmbedder

        return StubEmbedder()

    from app.analysis.embedder.gemini import GeminiEmbedder

    return GeminiEmbedder()
