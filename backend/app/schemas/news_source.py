"""news_sources CRUD エンドポイントの Pydantic スキーマ（SSoT）。"""

from datetime import datetime

from app.collection.domain.value_objects.source import SourceName
from app.models.news_source import SourceType
from app.schemas.base import _CamelBase
from app.shared.value_objects.safe_url import SafeUrl


class NewsSourceCreate(_CamelBase):
    """POST /api/v1/admin/sources のリクエストボディ。"""

    name: SourceName
    source_type: SourceType
    site_url: SafeUrl
    endpoint_url: SafeUrl


class NewsSourceDetail(_CamelBase):
    """API レスポンスにおける単一ニュースソース。"""

    id: int
    name: SourceName
    source_type: SourceType
    site_url: SafeUrl
    endpoint_url: SafeUrl
    is_active: bool
    created_at: datetime
    updated_at: datetime


class NewsSourceDetailList(_CamelBase):
    """GET /api/v1/admin/sources のレスポンスラッパー。"""

    items: list[NewsSourceDetail]
