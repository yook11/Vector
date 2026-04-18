from app.domain.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase
from app.schemas.embeds import TopicStatEmbed


class CategoryDetail(_CamelBase):
    """articleCount とネストしたトピックを備えたカテゴリ詳細。"""

    slug: CategorySlug
    name: CategoryName
    article_count: int = 0
    topics: list[TopicStatEmbed] = []


class CategoryDetailList(_CamelBase):
    """カテゴリ詳細一覧エンドポイント用のラッパー。"""

    items: list[CategoryDetail]
