"""Analysis ドメイン — AI による記事分析と埋め込みベクトル生成を担う。"""

from app.analysis.classification_service import (
    ClassificationResult,
    ClassificationService,
)
from app.analysis.classifier.base import BaseClassifier, ClassificationData
from app.analysis.classifier.factory import get_classifier
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
from app.analysis.extraction_service import ExtractionResult, ExtractionService
from app.analysis.extractor.base import BaseExtractor, ExtractionData
from app.analysis.extractor.factory import get_extractor

__all__ = [
    "AnalysisDomainError",
    "BaseClassifier",
    "BaseEmbedder",
    "BaseExtractor",
    "ClassificationData",
    "ClassificationResult",
    "ClassificationService",
    "ConfigurationError",
    "DailyQuotaExhaustedError",
    "EmbeddingResult",
    "EmbeddingService",
    "ExtractionData",
    "ExtractionResult",
    "ExtractionService",
    "InvalidInputError",
    "NetworkError",
    "ProviderError",
    "RateLimitError",
    "UnclassifiedError",
    "build_embed_text",
    "get_classifier",
    "get_embedder",
    "get_extractor",
]
