"""Project persisted agent runs into public research responses."""

from __future__ import annotations

from typing import Literal

from app.agent.runs.types import (
    AgentRunErrorCode,
    AgentRunProgressStage,
    AgentRunStatus,
)
from app.models.agent_run import AgentRun
from app.schemas.research import ResearchMessageRun, ResearchRunResponse

ResearchRunStatusValue = Literal["queued", "running", "completed", "failed"]
ResearchRunErrorCodeValue = Literal[
    "generation_unavailable",
    "internal_error",
    "enqueue_failed",
    "stale",
    "cancelled",
]
ResearchProgressStageValue = Literal["planning", "retrieving", "synthesizing"]


def build_research_run_response(*, run: AgentRun) -> ResearchRunResponse:
    return ResearchRunResponse(
        run_id=run.id,
        thread_id=run.thread_id,
        status=_run_status_value(run.status),
        error_code=_run_error_code_value(run.error_code),
        progress_stage=_run_progress_stage_value(run.progress_stage),
        attempt_epoch=run.attempt_epoch,
    )


def build_research_message_run(*, run: AgentRun) -> ResearchMessageRun:
    return ResearchMessageRun(
        run_id=run.id,
        status=_run_status_value(run.status),
        error_code=_run_error_code_value(run.error_code),
        progress_stage=_run_progress_stage_value(run.progress_stage),
    )


def _run_status_value(value: str) -> ResearchRunStatusValue:
    match AgentRunStatus(value):
        case AgentRunStatus.QUEUED:
            return "queued"
        case AgentRunStatus.RUNNING:
            return "running"
        case AgentRunStatus.COMPLETED:
            return "completed"
        case AgentRunStatus.FAILED:
            return "failed"


def _run_error_code_value(value: str | None) -> ResearchRunErrorCodeValue | None:
    if value is None:
        return None
    match AgentRunErrorCode(value):
        case AgentRunErrorCode.GENERATION_UNAVAILABLE:
            return "generation_unavailable"
        case AgentRunErrorCode.INTERNAL_ERROR:
            return "internal_error"
        case AgentRunErrorCode.ENQUEUE_FAILED:
            return "enqueue_failed"
        case AgentRunErrorCode.STALE:
            return "stale"
        case AgentRunErrorCode.CANCELLED:
            return "cancelled"


def _run_progress_stage_value(value: str | None) -> ResearchProgressStageValue | None:
    if value is None:
        return None
    match AgentRunProgressStage(value):
        case AgentRunProgressStage.PLANNING:
            return "planning"
        case AgentRunProgressStage.RETRIEVING:
            return "retrieving"
        case AgentRunProgressStage.SYNTHESIZING:
            return "synthesizing"
