import math
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _CamelBase(BaseModel):
    """Project-wide schema base with camelCase alias generation."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class PaginationParams(BaseModel):
    """Common pagination parameters.

    Consumed via ``Annotated[PaginationParams, Query()]`` (or a subclass
    thereof) in router signatures. Do not use with ``Depends()`` — that
    pattern silently drops non-scalar fields in subclasses (e.g. VO types)
    without emitting any warning. See feedback_vo_boundary memory.
    """

    page: Annotated[int, Query(ge=1)] = 1
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        return self.per_page

    def total_pages(self, total: int) -> int:
        return math.ceil(total / self.per_page) if total > 0 else 0
