from app.domain.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase
from app.schemas.embeds import TopicStatEmbed


class CategoryDetail(_CamelBase):
    """カテゴリ詳細。

    recentCount は直近 24 時間に AI 分類が完了した記事数。
    """

    slug: CategorySlug
    name: CategoryName
    recent_count: int = 0
    topics: list[TopicStatEmbed] = []


class CategoryDetailList(_CamelBase):
    """カテゴリ詳細一覧エンドポイント用のラッパー。"""

    items: list[CategoryDetail]
