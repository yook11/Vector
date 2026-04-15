"""Analysis domain — AI analysis, embedding, and deduplication."""

from app.analysis.analyzer.base import AnalysisData, AnalyzeResult, BaseAnalyzer
from app.analysis.analyzer.factory import get_analyzer
from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedder.factory import get_embedder
from app.analysis.errors import (
    AnalysisDomainError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.analysis.service import _build_embed_text, analyze_article, analyze_articles

__all__ = [
    "AnalysisData",
    "AnalysisDomainError",
    "AnalyzeResult",
    "BaseAnalyzer",
    "BaseEmbedder",
    "DailyQuotaExhaustedError",
    "InvalidInputError",
    "RateLimitError",
    "TransientError",
    "_build_embed_text",
    "analyze_article",
    "analyze_articles",
    "get_analyzer",
    "get_embedder",
]
