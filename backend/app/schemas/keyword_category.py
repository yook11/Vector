from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class KeywordCategoryBrief(BaseModel):
    """Minimal keyword category info embedded in KeywordResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    slug: str
    name: str


class KeywordCategoryResponse(BaseModel):
    """Full keyword category detail for GET /api/v1/keyword-categories."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    slug: str
    name: str


class KeywordCategoryListResponse(BaseModel):
    """Response wrapper for keyword category list endpoint."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[KeywordCategoryResponse]


class KeywordInCategory(BaseModel):
    """Keyword with article count, nested in category detail response."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    article_count: int = 0


class KeywordCategoryDetailResponse(BaseModel):
    """Enriched category with articleCount and nested keywords."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    slug: str
    name: str
    article_count: int = 0
    keywords: list[KeywordInCategory] = []


class KeywordCategoryDetailListResponse(BaseModel):
    """Response wrapper for enriched keyword category list endpoint."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[KeywordCategoryDetailResponse]
