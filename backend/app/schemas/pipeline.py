"""パイプライン操作（fetch / embed）用のスキーマ。"""

from app.schemas.base import _CamelBase


class FetchRequest(_CamelBase):
    """POST /api/v1/admin/pipeline/fetch のリクエストボディ。"""

    source_ids: list[int] | None = None


class FetchResponse(_CamelBase):
    """POST /api/v1/admin/pipeline/fetch のレスポンス。"""

    message: str
    sources_count: int | None = None
    job_id: str


class EmbedResponse(_CamelBase):
    """POST /api/v1/admin/pipeline/embed のレスポンス。"""

    message: str
    dispatched_count: int
