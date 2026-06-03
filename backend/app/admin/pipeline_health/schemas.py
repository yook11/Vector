"""全 audit stage の pipeline health projection API スキーマ。"""

from datetime import datetime

from pydantic import Field

from app.audit.domain.event import Stage
from app.schemas.base import _CamelBase

# ``_CamelBase`` の to_camel は ``..._24h`` を ``...24H`` (大文字 H) に変換する
# (str.title() が数字直後の英字を語頭扱いするため)。API 契約は小文字 ``24h`` の
# ため、該当フィールドだけ alias を明示して generator を上書きする。
_SUCCEEDED_24H_ALIAS = "succeededEventCount24h"
_FAILED_24H_ALIAS = "failedEventCount24h"


class PipelineStageHealth(_CamelBase):
    """1 stage の健全性スナップショット。

    全 audit stage を ``Stage`` enum の定義順で返す。queue は completion のみ、
    backfill は curation/assessment/embedding のみが値を持つ。意味を持たない軸は
    ``0`` / ``None`` を返す。
    """

    stage: Stage
    succeeded_event_count_24h: int = Field(
        validation_alias=_SUCCEEDED_24H_ALIAS,
        serialization_alias=_SUCCEEDED_24H_ALIAS,
    )
    failed_event_count_24h: int = Field(
        validation_alias=_FAILED_24H_ALIAS,
        serialization_alias=_FAILED_24H_ALIAS,
    )
    queue_count: int
    oldest_queue_age_seconds: int | None
    backfill_target_count: int
    oldest_backfill_target_age_seconds: int | None
    last_succeeded_at: datetime | None


class PipelineHealthSummary(_CamelBase):
    """全 stage を横断した集計サマリ。"""

    failed_event_count_24h: int = Field(
        validation_alias=_FAILED_24H_ALIAS,
        serialization_alias=_FAILED_24H_ALIAS,
    )
    backfill_target_total: int
    oldest_backfill_target_age_seconds: int | None
    completion_queue_count: int
    oldest_completion_queue_age_seconds: int | None
    observed_at: datetime
    event_window_start: datetime


class PipelineHealthResponse(_CamelBase):
    """GET /api/v1/admin/pipeline/health のレスポンス。"""

    summary: PipelineHealthSummary
    stages: list[PipelineStageHealth]
