"""Analyzer module — AI analysis abstraction, providers, and orchestration."""

from app.ai.analyzer.base import AnalysisData, AnalyzeResult, BaseAnalyzer
from app.ai.analyzer.errors import (
    AnalysisError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.ai.analyzer.factory import get_analyzer
from app.ai.analyzer.service import analyze_article, analyze_articles

__all__ = [
    "AnalysisData",
    "AnalysisError",
    "AnalyzeResult",
    "BaseAnalyzer",
    "DailyQuotaExhaustedError",
    "InvalidInputError",
    "RateLimitError",
    "TransientError",
    "analyze_article",
    "analyze_articles",
    "get_analyzer",
]
