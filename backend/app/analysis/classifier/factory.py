"""Classifier のファクトリ。"""

from __future__ import annotations

from app.analysis.classifier.base import BaseClassifier
from app.config import settings


def get_classifier() -> BaseClassifier:
    """``settings.ai_provider`` に応じた classifier インスタンスを返す。"""
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.classifier.gemini import GeminiClassifier

        return GeminiClassifier()
    if provider == "deepseek":
        from app.analysis.classifier.deepseek import DeepSeekClassifier

        return DeepSeekClassifier()
    raise ValueError(f"Unsupported AI provider: {provider}")
