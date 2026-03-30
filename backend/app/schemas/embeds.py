"""Lightweight schemas embedded inside other API responses.

These classes never appear as top-level API responses — they are always
nested within a parent response schema (e.g. NewsResponse, CategoryDetailResponse).
"""

from datetime import datetime

from app.domain.category import CategoryName, CategorySlug
from app.domain.keyword import KeywordName
from app.models.article_analysis import ImpactLevel
from app.schemas.base import _CamelBase


class CategoryEmbed(_CamelBase):
    """カテゴリの基本参照情報（slug + 名前）"""

    slug: CategorySlug
    name: CategoryName


class KeywordEmbed(_CamelBase):
    """キーワードの基本参照情報（カテゴリ付き）"""

    id: int
    name: KeywordName
    category: CategoryEmbed


class KeywordWithCountEmbed(_CamelBase):
    """キーワード＋記事数（カテゴリ内集計表示用）"""

    id: int
    name: KeywordName
    article_count: int = 0


class AnalysisEmbed(_CamelBase):
    """AI分析結果の要約"""

    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    ai_model: str
    analyzed_at: datetime
