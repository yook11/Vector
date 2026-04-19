"""Extractor のファクトリ。"""

from __future__ import annotations

from app.analysis.extraction.extractor.base import BaseExtractor
from app.config import settings


def get_extractor() -> BaseExtractor:
    """``settings.ai_provider`` に応じた extractor インスタンスを返す。"""
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.extraction.extractor.gemini import GeminiExtractor

        return GeminiExtractor()
    raise ValueError(f"Unsupported AI provider: {provider}")
