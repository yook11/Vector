"""Analysis ドメイン — AI による記事分析と埋め込みベクトル生成を担う。"""

from app.analysis.classification_service import (
    ClassificationResult,
    ClassificationService,
)
from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.factory import get_classifier
from app.analysis.classifier.schema import ClassificationResponse, ValidCategory
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
from app.analysis.extraction import (
    BaseExtractor,
    EntityResponse,
    ExtractionResponse,
    ExtractionResult,
    ExtractionService,
    get_extractor,
)

__all__ = [
    "AnalysisDomainError",
    "BaseClassifier",
    "BaseEmbedder",
    "BaseExtractor",
    "ClassificationResponse",
    "ClassificationResult",
    "ClassificationService",
    "ConfigurationError",
    "DailyQuotaExhaustedError",
    "EmbeddingResult",
    "EmbeddingService",
    "EntityResponse",
    "ExtractionResponse",
    "ExtractionResult",
    "ExtractionService",
    "InvalidInputError",
    "NetworkError",
    "ProviderError",
    "RateLimitError",
    "UnclassifiedError",
    "ValidCategory",
    "build_embed_text",
    "get_classifier",
    "get_embedder",
    "get_extractor",
]
