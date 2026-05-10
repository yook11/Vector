"""パイプライン Stage と EventType の StrEnum 定義。

CHECK 制約値 (``app/models/pipeline_event.py`` / migration) と一致させる。
不一致は ``test_pipeline_event_repository`` の set comparison test で検出。
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """パイプラインの 9 Stage。"""

    DISPATCH = "dispatch"
    SOURCE_FETCH = "source_fetch"
    CONTENT_FETCH = "content_fetch"
    EXTRACTION = "extraction"
    # PR4: 旧 CLASSIFICATION = "classification" を rename。enum メンバー名を完全削除
    # することで import 時に AttributeError で fail-fast し、移行漏れを検出する。
    ASSESSMENT = "assessment"
    EMBEDDING = "embedding"
    # NOTE: BACKFILL_CLASSIFY = "backfill_classify" は backfill 用の独立 stage 値で
    # PR4 では touch しない (旧 Stage.CLASSIFICATION とは別物)。
    BACKFILL_EXTRACT = "backfill_extract"
    BACKFILL_CLASSIFY = "backfill_classify"
    BACKFILL_EMBED = "backfill_embed"


class EventType(StrEnum):
    """イベント種別。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
