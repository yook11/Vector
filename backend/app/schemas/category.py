from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.domain.category import CategoryName, CategorySlug


class CategoryBrief(BaseModel):
    """Minimal category info embedded in KeywordResponse / KeywordBrief."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    slug: CategorySlug
    name: CategoryName


class KeywordInCategory(BaseModel):
    """Keyword with article count, nested in category detail response."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    article_count: int = 0


class CategoryDetailResponse(BaseModel):
    """Enriched category with articleCount and nested keywords."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    slug: CategorySlug
    name: CategoryName
    article_count: int = 0
    keywords: list[KeywordInCategory] = []


class CategoryDetailListResponse(BaseModel):
    """Response wrapper for enriched category list endpoint."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[CategoryDetailResponse]
