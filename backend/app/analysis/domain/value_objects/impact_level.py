"""ImpactLevel — 記事のインパクト水準を表すドメイン VO（値オブジェクト）。

Stage 2 分類で AI が割り当てる「業界へのインパクト水準」。
classifier / analysis 両方から参照される共有概念のため、
モデル層ではなくドメイン層の value_objects に配置する。

将来「impact_level に紐づくドメインメソッド」が必要になった時点で
RootModel ベースの VO に昇格させる余地を残しているが、
現状は StrEnum で十分（feedback_domain_over_implementation）。
"""

from __future__ import annotations

from enum import StrEnum


class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
