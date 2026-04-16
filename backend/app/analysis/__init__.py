"""Analysis domain — AI analysis and embedding."""

from app.analysis.analyzer.base import AnalysisData, BaseAnalyzer
from app.analysis.analyzer.factory import get_analyzer
from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedder.factory import get_embedder
from app.analysis.embedding_service import (
    EmbeddingResult,
    EmbeddingService,
    build_embed_text,
)
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.service import (
    AnalysisResult,
    ArticleAnalysisService,
    mark_article_skipped,
)

__all__ = [
    "AnalysisData",
    "AnalysisDomainError",
    "BaseAnalyzer",
    "BaseEmbedder",
    "ConfigurationError",
    "DailyQuotaExhaustedError",
    "InvalidInputError",
    "NetworkError",
    "ProviderError",
    "RateLimitError",
    "UnclassifiedError",
    "AnalysisResult",
    "ArticleAnalysisService",
    "EmbeddingResult",
    "EmbeddingService",
    "build_embed_text",
    "get_analyzer",
    "get_embedder",
    "mark_article_skipped",
]
