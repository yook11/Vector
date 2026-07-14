"""パイプライン操作（fetch）用のスキーマ。"""

from typing import Annotated

from pydantic import Field

from app.schemas.base import _CamelBase

_INT32_MAX = 2_147_483_647
_SourceId = Annotated[int, Field(ge=1, le=_INT32_MAX)]


class FetchRequest(_CamelBase):
    """POST /api/v1/admin/pipeline/fetch のリクエストボディ。"""

    # 配列上限は service-wide DoS 防御 (admin 認可と defense-in-depth)。
    # 現状の news_sources は ~20、将来拡張余地として 100 を採用。
    # 要素範囲は PostgreSQL INTEGER へのbind前に422で拒否する。
    source_ids: Annotated[list[_SourceId], Field(max_length=100)] | None = None


class FetchResponse(_CamelBase):
    """POST /api/v1/admin/pipeline/fetch のレスポンス。"""

    message: str
    dispatched_count: int | None = None
    job_id: str | None = None
