"""パイプライン Stage と EventType の StrEnum 定義。

CHECK 制約値 (``app/models/pipeline_event.py`` / migration) と一致させる。
不一致は ``test_pipeline_event_repository`` の set comparison test で検出。
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """パイプラインの 9 Stage。"""

    DISPATCH = "dispatch"
    ACQUISITION = "acquisition"
    COMPLETION = "completion"
    CURATION = "curation"
    ASSESSMENT = "assessment"
    EMBEDDING = "embedding"
    BACKFILL_CURATE = "backfill_curate"
    BACKFILL_ASSESS = "backfill_assess"
    BACKFILL_EMBED = "backfill_embed"


class EventType(StrEnum):
    """イベント種別。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
