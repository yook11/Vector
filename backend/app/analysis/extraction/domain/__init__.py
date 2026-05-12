"""extraction BC のドメイン層。

AI 分析の結果 (``Signal`` / ``Noise``、union alias ``ExtractionResult``) と、
システムに記録された分析結果 Entity (``Extraction``) を表現する。境界契約と
ドメイン不変条件を一本化し、振る舞い (sanitize・重複排除・identity) を
型に閉じ込める。
"""

from app.analysis.extraction.domain.entity import (
    EntityRawType,
    EntitySurface,
    ExtractedEntity,
)
from app.analysis.extraction.domain.extraction import (
    Extraction,
    ExtractionResult,
    Noise,
    Signal,
)
from app.analysis.extraction.domain.extraction_noise import ExtractionNoise

__all__ = [
    "EntityRawType",
    "EntitySurface",
    "ExtractedEntity",
    "Extraction",
    "ExtractionNoise",
    "ExtractionResult",
    "Noise",
    "Signal",
]
