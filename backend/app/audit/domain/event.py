"""パイプライン Stage と EventType の StrEnum 定義。

CHECK 制約値 (``app/models/pipeline_event.py`` / migration) と一致させる。
不一致は ``test_pipeline_event_repository`` の set comparison test で検出。
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """パイプラインの 10 Stage。

    BRIEFING は週次 LLM ブリーフィング生成 (``app/insights/briefing/``)。
    現状 Vector の ``pipeline_events`` で「briefing」が指す対象は週次 briefing 以外
    に存在しない (snapshot は別 stage、daily 等は未導入)。将来 daily 等を入れる
    ことになったら z1/z4 と同型の rename migration で対処する。
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


class EventType(StrEnum):
    """イベント種別。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
