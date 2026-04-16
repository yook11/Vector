"""Analyzer のファクトリ。"""

from __future__ import annotations

from app.analysis.analyzer.base import BaseAnalyzer
from app.config import settings


def get_analyzer() -> BaseAnalyzer:
    """``settings.ai_provider`` に応じた analyzer インスタンスを返すファクトリ。

    Raises:
        ValueError: サポートされていない ai_provider が指定された場合。
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.analyzer.gemini import GeminiAnalyzer

        return GeminiAnalyzer()
    raise ValueError(f"Unsupported AI provider: {provider}")
