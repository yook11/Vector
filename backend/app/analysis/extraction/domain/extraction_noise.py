"""Stage 1 noise 判定された記事の Domain Entity。

``ExtractionNoise`` は ``relevance="noise"`` だった記事の永続化記録を
表す Entity。signal 側 (``Extraction``) との対称性を保ちつつ、entities は
JSONB 1 カラムでの内包保持に対応した構造で持つ。

entities の型は signal/noise で同一概念 (Stage 1 観察台帳の 1 行) のため
``ExtractedEntity`` を再利用する。JSONB の配列インデックスがそのまま
順序を保証するため、signal 側の ``ArticleExtractionEntity.position``
相当は持たない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.analysis.extraction.domain.entity import ExtractedEntity

__all__ = ["ExtractionNoise"]


@dataclass(frozen=True, slots=True)
class ExtractionNoise:
    """Stage 1 で ``relevance="noise"`` と判定され永続化された記事 Entity。

    Invariants:
    - id は DB が採番した正の整数
    - title_ja / summary_ja / ai_model は非空 (DB CHECK 制約と一致)
    - entities は ``(surface.match_key, raw_type.root)`` で重複なし
      (``ExtractionResult`` 通過時点で保証済みだが、DB 復元時の安全網)
    """

    id: int
    article_id: int
    title_ja: str
    summary_ja: str
    entities: tuple[ExtractedEntity, ...]
    ai_model: str
    rejected_at: datetime

    def __post_init__(self) -> None:
        if not self.title_ja:
            raise ValueError("ExtractionNoise.title_ja must be non-empty")
        if not self.summary_ja:
            raise ValueError("ExtractionNoise.summary_ja must be non-empty")
        if not self.ai_model:
            raise ValueError("ExtractionNoise.ai_model must be non-empty")
        seen: set[tuple[str, str]] = set()
        for e in self.entities:
            key = e.dedup_key()
            if key in seen:
                raise ValueError(
                    f"ExtractionNoise.entities must be deduplicated, "
                    f"duplicated: {key!r}"
                )
            seen.add(key)
