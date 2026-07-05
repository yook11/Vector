"""Question plan draft returned by LLM adapters."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract import RetrievalMode


class QuestionPlanDraft(BaseModel):
    """Planner-internal draft parsed from structured LLM output."""

    model_config = ConfigDict(frozen=True)

    retrieval_mode: RetrievalMode
    internal_queries: list[str] = Field(default_factory=list)
    external_collection_goals: list[str] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str = Field(min_length=1)
