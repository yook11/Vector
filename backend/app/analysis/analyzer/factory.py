"""Analyzer factory."""

from __future__ import annotations

from app.analysis.analyzer.base import BaseAnalyzer
from app.config import settings


def get_analyzer() -> BaseAnalyzer:
    """Factory: return an analyzer instance based on settings.ai_provider.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.analyzer.gemini import GeminiAnalyzer

        return GeminiAnalyzer()
    raise ValueError(f"Unsupported AI provider: {provider}")
