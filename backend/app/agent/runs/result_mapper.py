"""Map completed agent results to persisted conversation rows."""

from __future__ import annotations

from uuid import UUID

from app.agent.contract import (
    AnswerQuestionResult,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.models.agent_message import AgentMessage, AgentMessageSource


def build_assistant_message_for_result(
    *,
    thread_id: UUID,
    seq: int,
    result: AnswerQuestionResult,
) -> AgentMessage:
    return AgentMessage(
        thread_id=thread_id,
        seq=seq,
        role="assistant",
        content=result.answer,
        missing_aspects=list(result.missing_aspects),
    )


def build_source_rows_for_message(
    message: AgentMessage,
    result: AnswerQuestionResult,
) -> list[AgentMessageSource]:
    if message.role != "assistant":
        raise ValueError("agent sources can only be attached to assistant messages")

    rows: list[AgentMessageSource] = []
    for ordinal, source in enumerate(result.sources, start=1):
        match source:
            case InternalArticleSource():
                rows.append(
                    AgentMessageSource(
                        message_id=message.id,
                        ordinal=ordinal,
                        kind=source.kind,
                        source_ref=source.source_ref,
                        analyzed_article_id=source.article_id,
                        url=None,
                        title=source.title,
                        source_name=None,
                        published_at=source.published_at,
                        evidence_claim=None,
                    )
                )
            case ExternalUrlSource():
                rows.append(
                    AgentMessageSource(
                        message_id=message.id,
                        ordinal=ordinal,
                        kind=source.kind,
                        source_ref=source.source_ref,
                        analyzed_article_id=None,
                        url=str(source.url),
                        title=source.title,
                        source_name=source.source_name,
                        published_at=source.published_at,
                        evidence_claim=source.evidence_claim,
                    )
                )
    return rows
