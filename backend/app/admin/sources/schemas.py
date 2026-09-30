"""news_sources CRUD エンドポイントの Pydantic スキーマ（SSoT）。"""

from datetime import datetime

from app.collection.sources.source_name import SourceName
from app.models.news_source import SourceType
from app.schemas.base import _CamelBase
from app.shared.web_url import WebUrl


class NewsSourceCreate(_CamelBase):
    """POST /api/v1/admin/sources のリクエストボディ。"""

    name: SourceName
    source_type: SourceType
    site_url: WebUrl
    endpoint_url: WebUrl


class NewsSourceDetail(_CamelBase):
    """API レスポンスにおける単一ニュースソース。"""

    id: int
    name: SourceName
    source_type: SourceType
    site_url: WebUrl
    endpoint_url: WebUrl
    is_active: bool
    attribution_label: str | None = None
    created_at: datetime
    updated_at: datetime


class NewsSourceDetailList(_CamelBase):
    """GET /api/v1/admin/sources のレスポンスラッパー。"""

    items: list[NewsSourceDetail]
