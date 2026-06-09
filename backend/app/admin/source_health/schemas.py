"""ソース別 health 観測 API のスキーマ（SSoT）。"""

from datetime import datetime
from enum import IntEnum

from app.models.news_source import SourceType
from app.schemas.base import _CamelBase


class WindowHours(IntEnum):
    """表示窓 (時間)。query string を coerce して許可値外を 422 で弾く。

    ``Literal[int]`` は query 文字列を int に coerce せず全値が落ちるため IntEnum。
    """

    H24 = 24
    H48 = 48
    H72 = 72
    H168 = 168


class FailureReason(_CamelBase):
    """選択窓内の outcome code 別失敗・棄却件数。"""

    outcome_code: str
    count: int


class SourceHealthItem(_CamelBase):
    """1 ニュースソースの health スナップショット。

    name / type / active は source を識別する基本情報であり、analyzable rate
    以降の窓依存指標とは別軸。incomplete count と last succeeded at は表示窓に
    依存しない現在値。free-text error / URL / payload 詳細は持たない。
    """

    source_id: int
    source_name: str
    source_type: SourceType
    is_active: bool
    analyzable_rate: float | None
    analyzable_count: int
    processed_article_count: int
    incomplete_count: int
    failure_reasons: list[FailureReason]
    last_succeeded_at: datetime | None


class SourceHealthResponse(_CamelBase):
    """GET /api/v1/admin/sources/health のレスポンス。"""

    window_hours: int
    observed_at: datetime
    items: list[SourceHealthItem]
