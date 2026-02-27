from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CategoryBrief(BaseModel):
    """Minimal category info embedded in AnalysisResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    slug: str
    name: str


class CategoryResponse(BaseModel):
    """Full category detail for GET /api/v1/categories."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    slug: str
    name: str
    description: str | None = None


class CategoryListResponse(BaseModel):
    """Response wrapper for category list endpoint."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[CategoryResponse]
