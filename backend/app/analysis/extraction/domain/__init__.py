"""extraction BC のドメイン層。

AI 分析の結果 (``ExtractionResult``) と、システムに記録された分析結果 Entity
(``Extraction``) を表現する。境界契約とドメイン不変条件を一本化し、
振る舞い (sanitize・重複排除・identity) を型に閉じ込める。
"""

from app.analysis.extraction.domain.entity import (
    EntityRawType,
    EntitySurface,
    ExtractedEntity,
)
from app.analysis.extraction.domain.extraction import Extraction, ExtractionResult

__all__ = [
    "EntityRawType",
    "EntitySurface",
    "ExtractedEntity",
    "Extraction",
    "ExtractionResult",
]
