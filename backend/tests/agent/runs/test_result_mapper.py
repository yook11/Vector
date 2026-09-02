"""回答結果から assistant message / source 行を組む契約。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.agent.runs.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.threads.projection import build_research_assistant_message
from app.models.agent_message import AgentMessage
from app.shared.security.safe_url import SafeUrl


def _plan_summary(plan_type: str) -> AnswerPlanSummary:
    return AnswerPlanSummary(plan_type=plan_type)


def _direct_result(answer: str = "worker answer") -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer=answer,
        sources=[],
        missing_aspects=[],
        plan_summary=_plan_summary("direct_answer"),
    )


def test_source_mapper_rejects_user_message() -> None:
    message = AgentMessage(
        thread_id=UUID("00000000-0000-4000-a000-000000000001"),
        seq=1,
        role="user",
        content="question",
        missing_aspects=[],
    )

    with pytest.raises(ValueError, match="assistant messages"):
        build_source_rows_for_message(message, _direct_result())


def test_source_mapper_structures_internal_and_external_rows() -> None:
    result = AnswerQuestionResult(
        status="answered",
        answer="answer [[1]][[2]]",
        sources=[
            InternalArticleSource(source_ref="1", article_id=123, title="Internal"),
            ExternalUrlSource(
                source_ref="2",
                url=SafeUrl("https://example.com/e"),
                title="External",
                evidence_claim="Claim",
            ),
        ],
        missing_aspects=[],
        plan_summary=_plan_summary("search"),
    )
    message = build_assistant_message_for_result(
        thread_id=UUID("00000000-0000-4000-a000-000000000001"),
        seq=2,
        result=result,
    )
    message.id = UUID("00000000-0000-4000-a000-000000000010")
    message.created_at = datetime(2026, 7, 9, tzinfo=UTC)

    rows = build_source_rows_for_message(message, result)

    assert message.role == "assistant"
    assert message.content == "answer [[1]][[2]]"
    assert rows[0].analyzed_article_id == 123
    assert rows[0].url is None
    assert rows[0].evidence_claim is None
    assert rows[1].url == "https://example.com/e"
    assert rows[1].analyzed_article_id is None
    assert rows[1].evidence_claim == "Claim"

    response = build_research_assistant_message(message=message, sources=rows)
    assert response.content == "answer [[1]][[2]]"
    assert response.sources[0].kind == "internal_article"
    assert response.sources[1].kind == "external_url"
