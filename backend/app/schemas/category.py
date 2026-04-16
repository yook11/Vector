from app.domain.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase
from app.schemas.embeds import KeywordStatEmbed


class CategoryDetail(_CamelBase):
    """articleCount とネストしたキーワードを備えたカテゴリ詳細。"""

    slug: CategorySlug
    name: CategoryName
    article_count: int = 0
    keywords: list[KeywordStatEmbed] = []


class CategoryDetailList(_CamelBase):
    """カテゴリ詳細一覧エンドポイント用のラッパー。"""

    items: list[CategoryDetail]
