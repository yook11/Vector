"""Lightweight schemas embedded inside other API responses.

These classes never appear as top-level API responses — they are always
nested within a parent response schema (e.g. NewsBrief, CategoryDetail).
"""

from app.domain.category import CategoryName, CategorySlug
from app.domain.keyword import KeywordName
from app.domain.safe_url import SafeUrl
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


class KeywordStatEmbed(_CamelBase):
    """キーワード＋記事数（カテゴリ内集計表示用）"""

    id: int
    name: KeywordName
    article_count: int = 0


class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報（詳細画面用）"""

    title: str
    url: SafeUrl
    content: str | None = None
