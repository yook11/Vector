"""Stage 2 分類 AI レスポンスの Pydantic スキーマ。

ClassificationResponse は Gemini SDK の ``response_schema`` に渡され、
受信時に構造・列挙妥当性を保証する境界型。ValidCategory は AI が
出力可能なカテゴリを列挙し、StrEnum として AI 境界専用に利用する。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.topic import TopicName
from app.models.article_analysis import ImpactLevel


class ValidCategory(StrEnum):
    """AI が出力可能なカテゴリ slug。

    ``CategorySlug`` とは目的が異なる: こちらは AI 境界で許容する
    値の集合、``CategorySlug`` は slug 書式の任意文字列を表す VO。
    """

    AI = "ai"
    BIO = "bio"
    COMPUTING = "computing"
    ENERGY = "energy"
    MATERIALS = "materials"
    NETWORK = "network"
    ROBOTICS = "robotics"
    SECURITY = "security"
    SEMICONDUCTOR = "semiconductor"
    SPACE = "space"


class ClassificationResponse(BaseModel):
    """Stage 2 分類の構造化レスポンス。

    Invariants:
    - category は ValidCategory の列挙値
    - topic は TopicName として正規化・検証済み
    - impact_level は ImpactLevel の列挙値
    """

    model_config = ConfigDict(frozen=True)

    category: ValidCategory
    topic: TopicName
    impact_level: ImpactLevel
    reasoning: str = Field(default="")
