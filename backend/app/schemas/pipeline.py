"""Schemas for pipeline operations (fetch, embed)."""

from app.schemas.base import _CamelBase


class FetchRequest(_CamelBase):
    """POST /api/v1/admin/pipeline/fetch request body."""

    source_ids: list[int] | None = None


class FetchResponse(_CamelBase):
    """POST /api/v1/admin/pipeline/fetch response."""

    message: str
    sources_count: int | None = None
    job_id: str


class EmbedResponse(_CamelBase):
    """POST /api/v1/admin/pipeline/embed response."""

    message: str
    embedded_count: int
    skipped_count: int
    error_count: int
