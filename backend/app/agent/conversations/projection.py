"""Project persisted conversation rows into public research responses."""

from __future__ import annotations

from uuid import UUID

from app.agent.runs.projection import build_research_message_run
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import (
    ResearchAssistantMessage,
    ResearchExternalUrlSource,
    ResearchInternalArticleSource,
    ResearchSource,
    ResearchThreadDetail,
    ResearchThreadListItem,
    ResearchThreadMessage,
    ResearchUserMessage,
)
from app.shared.security.safe_url import SafeUrl


def build_research_thread_list_item(
    *,
    thread: AgentThread,
    has_active_run: bool,
) -> ResearchThreadListItem:
    return ResearchThreadListItem(
        thread_id=thread.id,
        title=thread.title,
        updated_at=thread.updated_at,
        has_active_run=has_active_run,
    )


def build_research_thread_detail(
    *,
    thread: AgentThread,
    messages: list[AgentMessage],
    runs_by_user_message_id: dict[UUID, AgentRun],
    sources_by_message_id: dict[UUID, list[AgentMessageSource]],
) -> ResearchThreadDetail:
    return ResearchThreadDetail(
        thread_id=thread.id,
        title=thread.title,
        messages=[
            _message_response(
                message,
                runs_by_user_message_id=runs_by_user_message_id,
                sources_by_message_id=sources_by_message_id,
            )
            for message in messages
        ],
    )


def build_research_assistant_message(
    *,
    message: AgentMessage,
    sources: list[AgentMessageSource],
) -> ResearchAssistantMessage:
    return ResearchAssistantMessage(
        role="assistant",
        seq=message.seq,
        content=message.content,
        created_at=message.created_at,
        sources=[_source_response(source) for source in sources],
        missing_aspects=list(message.missing_aspects),
    )


def _message_response(
    message: AgentMessage,
    *,
    runs_by_user_message_id: dict[UUID, AgentRun],
    sources_by_message_id: dict[UUID, list[AgentMessageSource]],
) -> ResearchThreadMessage:
    if message.role == "user":
        run = runs_by_user_message_id.get(message.id)
        if run is None:
            raise ValueError("user message is missing its agent run")
        return ResearchUserMessage(
            role="user",
            seq=message.seq,
            content=message.content,
            created_at=message.created_at,
            run=build_research_message_run(run=run),
        )
    if message.role != "assistant":
        raise ValueError(f"unknown agent message role: {message.role!r}")
    return build_research_assistant_message(
        message=message,
        sources=sources_by_message_id.get(message.id, []),
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
