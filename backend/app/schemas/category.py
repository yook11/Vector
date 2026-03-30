from app.domain.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase
from app.schemas.embeds import KeywordWithCountEmbed


class CategoryDetailResponse(_CamelBase):
    """Enriched category with articleCount and nested keywords."""

    id: int
    slug: CategorySlug
    name: CategoryName
    article_count: int = 0
    keywords: list[KeywordWithCountEmbed] = []


class CategoryDetailListResponse(_CamelBase):
    """Response wrapper for enriched category list endpoint."""

    items: list[CategoryDetailResponse]
