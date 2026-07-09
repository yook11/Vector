"""Project persisted agent history rows into public research responses."""

from __future__ import annotations

from typing import Literal

from app.agent.history.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.schemas.research import (
    ResearchExternalUrlSource,
    ResearchInternalArticleSource,
    ResearchResponse,
    ResearchRunResponse,
    ResearchSource,
)
from app.shared.security.safe_url import SafeUrl

ResearchRunStatusValue = Literal["queued", "running", "completed", "failed"]
ResearchRunErrorCodeValue = Literal[
    "generation_unavailable",
    "internal_error",
    "enqueue_failed",
    "stale",
]


def build_research_run_response(
    *,
    run: AgentRun,
    result: ResearchResponse | None,
) -> ResearchRunResponse:
    return ResearchRunResponse(
        run_id=run.id,
        thread_id=run.thread_id,
        status=_run_status_value(run.status),
        result=result,
        error_code=_run_error_code_value(run.error_code),
    )


def build_research_response_from_rows(
    *,
    message: AgentMessage,
    sources: list[AgentMessageSource],
) -> ResearchResponse:
    return ResearchResponse(
        answer=message.content,
        sources=[_source_response(source) for source in sources],
        missing_aspects=list(message.missing_aspects),
    )


def _source_response(source: AgentMessageSource) -> ResearchSource:
    if source.kind == "internal_article":
        return ResearchInternalArticleSource(
            kind="internal_article",
            source_ref=source.source_ref,
            article_id=source.analyzed_article_id,
            title=source.title,
            published_at=source.published_at,
        )
    if source.kind != "external_url":
        raise ValueError(f"unknown agent source kind: {source.kind!r}")
    if source.url is None or source.evidence_claim is None:
        raise ValueError("external_url source row is missing required fields")
    return ResearchExternalUrlSource(
        kind="external_url",
        source_ref=source.source_ref,
        url=SafeUrl(source.url),
        title=source.title,
        source_name=source.source_name,
        published_at=source.published_at,
        evidence_claim=source.evidence_claim,
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
