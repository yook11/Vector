"""pipeline_events.payload の stage 別 Pydantic payload。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    dispatched_count: int | None = None
    selected_count: int | None = None
    rejected_count: int | None = None
    failed_count: int | None = None
    raw_source_name: str | None = None
    skip_reason: Literal["no_active_sources"] | None = None


class BackfillPayload(BasePipelineEventPayload):
    """Backfill stage payload。"""

    kind: Literal["backfill"] = "backfill"
    backfill_stage: Literal["curate", "assess", "embed"]
    run_id: str | None = None
    target_kind: Literal["article", "curation", "analysis"] | None = None
    target_id: int | None = None
    selected_count: int | None = None
    granted_count: int | None = None
    enqueued_count: int | None = None
    failed_count: int | None = None
    limit: int | None = None
    daily_max: int | None = None


class AcquisitionPayload(BasePipelineEventPayload):
    """Stage 1 acquisition payload。"""

    kind: Literal["acquisition"] = "acquisition"
    failure_kind: str | None = None
    failure_action: str | None = None
    fetcher_class: str | None = None

    # acquisition / completion をまたぐ canonical URL key。
    canonical_url: str | None = None

    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None

    # 接続失敗 (fetch origin) の specifics。http_status (上の HTTP snapshot 列) と対称に
    # outcome_code = CODE とは別に reason / Retry-After を復元できるようにする。
    fetch_reason: str | None = None
    fetch_retry_after_seconds: float | None = None

    # read 失敗 (reader 構造化不能) の specifics。outcome_code = reason.value とは別に
    # どの形式 / どのフィールド / どの位置で落ちたかを残す。
    read_format: str | None = None
    read_field: str | None = None
    read_parser_position: str | None = None

    conversion_raw_url: str | None = None
    conversion_has_title: bool | None = None
    conversion_body_length: int | None = None
    conversion_has_published_at: bool | None = None


class CompletionPayload(BasePipelineEventPayload):
    """Stage 2 completion payload。"""

    kind: Literal["completion"] = "completion"
    failure_kind: str | None = None
    failure_action: str | None = None
    pending_id: int | None = None
    pending_status: str | None = None
    canonical_url: str | None = None
    # completion の claim / retry 制御に由来する snapshot。
    attempt_count: int | None = None
    scraper_class: str | None = None
    reason_code: str | None = None
    # 完成段ドメイン棄却の defect 全集合 (outcome_code は主 defect = defects[0])。
    defects: list[str] | None = None
    body_length: int | None = None
    quality_gate_metric: dict[str, Any] | None = None
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None
    # body_head に prompt-injection 境界タグを検出した行だけ True。
    injection_markers_present: bool | None = None
    # retry 上限に到達して諦めた行だけ True。
    retry_exhausted: bool | None = None


class CurationPayload(BasePipelineEventPayload):
    """Stage 3 — 大きい入力 (記事本文) は head + length + hash で扱う (curation)。"""

    kind: Literal["curation"] = "curation"
    failure_kind: str | None = None
    failure_action: str | None = None
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
    """Stage 5 — analysis テキストから vector を生成。

    raw I/O 捕捉は不要 (入力は analysis に永続、出力は数値 vector で
    injection 観点無関係)。
    """

    kind: Literal["embedding"] = "embedding"
    failure_kind: str | None = None
    failure_action: str | None = None
    analysis_id: int | None = None
    embedding_model: str | None = None
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
    trigger: Literal["cron", "cli"] | None = None
    requested_update: bool | None = None
    source_analysis_count: int | None = None
    completed_category_count: int | None = None


PipelineEventPayload = Annotated[
    DispatchPayload
    | BackfillPayload
    | AcquisitionPayload
    | CompletionPayload
    | CurationPayload
    | AssessmentPayload
    | EmbeddingPayload
    | BriefingPayload
    | TrendDiscoveryPayload,
    Field(discriminator="kind"),
]
