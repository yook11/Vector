"""pipeline_events.payload の stage 別 Pydantic payload。"""

from __future__ import annotations

from typing import Annotated, Literal

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
    raw_source_name: str | None = None
    skip_reason: Literal["no_active_sources"] | None = None


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
    DispatchPayload | BriefingPayload | TrendDiscoveryPayload,
    Field(discriminator="kind"),
]
