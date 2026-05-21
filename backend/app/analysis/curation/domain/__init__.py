"""curation BC のドメイン層。

AI 分析の結果 (``Signal`` / ``Noise``、union alias ``CurationResult``) を
表現する。AI 境界での sanitize を ``Signal`` / ``Noise`` の
validator に閉じ込め、下流 Stage に「HTML 抜き、NFKC 済、非空」を保証する。

永続化結果 (``article_extractions`` / ``extraction_noises`` の 1 行) は
``CurationRepository`` が ``int`` id として返し、Domain Entity 化はしない
(Stage 4 Assessment / Stage 5 Embedding と対称な勝者 SSoT パターン)。
"""

from app.analysis.curation.domain.result import (
    CurationResult,
    Noise,
    Signal,
)

__all__ = [
    "CurationResult",
    "Noise",
    "Signal",
]
