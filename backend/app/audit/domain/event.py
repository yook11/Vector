"""パイプライン Stage と EventType の StrEnum 定義。

CHECK 制約値 (``app/models/pipeline_event.py`` / migration) と一致させる。
不一致は ``test_pipeline_event_repository`` の set comparison test で検出。
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """パイプラインの 11 Stage。

    BRIEFING は週次 LLM ブリーフィング生成 (``app/insights/briefing/``)。
    TREND_DISCOVERY は rolling 7d の trend discovery run
    (``app/insights/trend_discovery/``)。
    """

    DISPATCH = "dispatch"
    ACQUISITION = "acquisition"
    COMPLETION = "completion"
    CURATION = "curation"
    ASSESSMENT = "assessment"
    EMBEDDING = "embedding"
    BACKFILL_CURATE = "backfill_curate"
    BACKFILL_ASSESS = "backfill_assess"
    BACKFILL_EMBED = "backfill_embed"
    BRIEFING = "briefing"
    TREND_DISCOVERY = "trend_discovery"


class EventType(StrEnum):
    """イベント種別。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
