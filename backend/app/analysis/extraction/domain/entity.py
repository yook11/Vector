"""ExtractedEntity — Stage 1 観察台帳の 1 行を表す複合 VO。

VO (``EntitySurface`` / ``EntityRawType``) 自体の実装は
``app.analysis.domain.value_objects.entity`` に置き、本モジュールでは
それらを束ねた複合 VO ``ExtractedEntity`` を定義する。VO を value_objects 階層に
置く理由は、SQLAlchemy ``TypeDecorator`` (``app.models.types``) が
``app.models.base`` 経由で読み込まれる際の循環 import を避けるため。

設計差分 (旧 ``Entity`` から):

- ``surface`` (= ``EntitySurface``): 旧 ``name`` と同じ不変条件 (NFKC + 空白整形 +
  casing 保持 + 200 字 + ``match_key``)。``EntitySurface`` は ``EntityName`` の
  alias であり、新規 VO は作らない (memory:
  ``feedback_no_share_different_problems.md`` の逆 — 同じ問題なら共用)。
- ``raw_type`` (= ``EntityRawType``): 旧 ``type`` (``EntityType``) と異なり、
  - 上限 30 字 (Stage 1 観察ラベルとして実態に合わせた)
  - 小文字化 **しない** (``casing`` を保持して β の canonical_type と衝突回避)
  - ``match_key`` を **持たない** (β の集計と直接合流させない設計の表明)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.domain.value_objects.entity import (
    EntityRawType,
    EntitySurface,
)

__all__ = ["EntityRawType", "EntitySurface", "ExtractedEntity"]


class ExtractedEntity(BaseModel):
    """Stage 1 観察台帳の 1 行 (surface + raw_type の複合 VO)。

    Invariants:
    - surface / raw_type は各 VO の不変条件を満たす
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    surface: EntitySurface = Field(
        description=(
            "Exact surface form of the entity as written in the article "
            "(preserve original casing)."
        )
    )
    raw_type: EntityRawType = Field(
        description=(
            "Short lowercase English label for the entity type, such as "
            '"company", "person", "product", "service", "technology", '
            'or "institution".'
        )
    )

    def dedup_key(self) -> tuple[str, str]:
        """同一エンティティ判定キー。

        surface 側は ``match_key`` (str.lower()) で casing 違いを吸収する
        ("NVIDIA" と "nvidia" は同一)。raw_type 側は ``root`` をそのまま使い
        casing 違いを別エンティティとして扱う (β の canonical_type 集計と
        独立した観察値として保持する設計)。
        """
        return (self.surface.match_key, self.raw_type.root)
