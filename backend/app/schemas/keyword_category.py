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
