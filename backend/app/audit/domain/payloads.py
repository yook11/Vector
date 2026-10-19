"""pipeline_events.payload の stage 別 Pydantic payload。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collection.sources.source_name import SourceName


class BasePipelineEventPayload(BaseModel):
    """全 payload variant の共通 field。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str
    source_name: str | None = None
    error_message: str | None = None
    error_chain: list[str] | None = None


class DispatchPayload(BasePipelineEventPayload):
    """Dispatch stage payload。"""

    kind: Literal["dispatch"] = "dispatch"
    cadence: Literal["high", "medium", "low", "all"] | None = None
    raw_source_name: str | None = None
    skip_reason: Literal["no_active_sources"] | None = None


class BackfillPayload(BasePipelineEventPayload):
    """Backfill stage payload。"""

    kind: Literal["backfill"] = "backfill"
    source_name: SourceName | None = None
    backfill_stage: Literal["curate", "assess", "embed", "complete"]
    run_id: str | None = None
    target_kind: (
        Literal["article", "curation", "analyzed_article", "incomplete_article"] | None
    ) = None
    target_id: int | None = None
    # daily_max は budget exhausted event 専用の「停止の閾値」(B 級 config snapshot)。
    daily_max: int | None = None


class CurationPayload(BasePipelineEventPayload):
    """Stage 3 — 大きい入力 (記事本文) は head + length + hash で扱う (curation)。"""

    kind: Literal["curation"] = "curation"
    failure_kind: str | None = None
    failure_action: str | None = None
    # プロバイダーのreasonを保持し、回復分類は記録しない。
    failure_reason: str | None = None
    target_article_id: int | None = None
    ai_model: str | None = None
    prompt_version: str | None = None

    input_content_head: str | None = None
    input_content_length: int | None = None
    max_content_length: int | None = None
    input_content_hash: str | None = None
    # input_content に prompt-injection 境界タグを検出した行だけ True。
    injection_markers_present: bool | None = None
    ai_raw_response: str | None = None
    raw_relevance: str | None = None


class AssessmentPayload(BasePipelineEventPayload):
    """Stage 4 assessment payload。"""

    kind: Literal["assessment"] = "assessment"
    failure_kind: str | None = None
    failure_action: str | None = None
    # プロバイダーのreasonを保持し、回復分類は記録しない。
    failure_reason: str | None = None

    curation_id: int | None = None

    ai_model: str | None = None
    prompt_version: str | None = None
    input_text: str | None = None
    input_text_length: int | None = None
    ai_raw_response: str | None = None
    raw_category: str | None = None
    category_slug: str | None = None
    investor_take: str | None = None


class EmbeddingPayload(BasePipelineEventPayload):
    """Stage 5 — analyzed article テキストから vector を生成。

    raw I/O 捕捉は不要 (入力は analyzed article に永続、出力は数値 vector で
    injection 観点無関係)。
    """

    kind: Literal["embedding"] = "embedding"
    failure_kind: str | None = None
    failure_action: str | None = None
    # 原因詳細 (provider error の reason 値)。provider 由来でない失敗は None。
    failure_reason: str | None = None
    analyzed_article_id: int | None = None
    ai_model: str | None = None
    vector_dimension: int | None = None


class BriefingPayload(BasePipelineEventPayload):
    """Briefing stage payload。"""

    kind: Literal["briefing"] = "briefing"
    failure_kind: str | None = None
    failure_action: str | None = None
    week_start: str | None = None
    category_id: int | None = None
    category_slug: str | None = None
    article_count: int | None = None
    category_count: int | None = None
    selected_category_count: int | None = None
    enqueued_category_count: int | None = None
    failed_category_count: int | None = None
    ai_model: str | None = None
    retry_exhausted: bool | None = None


class TrendDiscoveryPayload(BasePipelineEventPayload):
    """Trend discovery stage payload。"""

    kind: Literal["trend_discovery"] = "trend_discovery"
    window_start: str | None = None
    window_end: str | None = None
    source_analysis_count: int | None = None
    completed_category_count: int | None = None


PipelineEventPayload = Annotated[
    DispatchPayload
    | BackfillPayload
    | CurationPayload
    | AssessmentPayload
    | EmbeddingPayload
    | BriefingPayload
    | TrendDiscoveryPayload,
    Field(discriminator="kind"),
]
