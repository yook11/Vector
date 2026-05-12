"""Extraction — Stage 3 事実抽出パッケージ。

``ai/`` 配下 (``BaseExtractor`` / ``GeminiExtractor`` / ``GeminiExtractionPrompt`` /
``ExtractionCall``) は BC 内部実装として閉じる。AI provider 抽象を取りたい場合は
``app.analysis.extraction.ai.base`` 等の深い path から取得すること。
``embedding`` / ``assessment`` パッケージとの対称性 (ai/ 配下を ``__init__.py`` から
re-export しない) を維持する。
"""

from app.analysis.extraction.domain import (
    EntityRawType,
    EntitySurface,
    ExtractedEntity,
    Extraction,
    ExtractionResult,
    Noise,
    Signal,
)
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.repository import ExtractionRepository
from app.analysis.extraction.service import (
    ExtractedOutcome,
    ExtractionOutcome,
    ExtractionService,
    NoiseOutcome,
)

__all__ = [
    "EntityRawType",
    "EntitySurface",
    "ExtractedEntity",
    "ExtractedOutcome",
    "Extraction",
    "ExtractionOutcome",
    "ExtractionRepository",
    "ExtractionResult",
    "ExtractionService",
    "Noise",
    "NoiseOutcome",
    "ReadyForExtraction",
    "Signal",
]
