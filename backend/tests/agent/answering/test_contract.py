"""Shared answering contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.question_context.contract import AnswerBrief


def test_answering_request_is_a_frozen_answer_brief_consumer_wrapper() -> None:
    answer_brief = AnswerBrief(standalone_question="NVIDIA の直近発表は？")
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    request = AnsweringRequest(answer_brief=answer_brief, as_of=as_of)

    with pytest.raises(ValidationError):
        request.answer_brief = AnswerBrief(standalone_question="別の質問")
    with pytest.raises(ValidationError):
        AnsweringRequest(
            answer_brief=answer_brief, as_of=as_of, previous_answer="前の回答"
        )
    with pytest.raises(ValidationError):
        AnsweringRequest(context=answer_brief, as_of=as_of)

    assert (
        set(AnsweringRequest.model_fields),
        AnsweringRequest.model_fields["answer_brief"].annotation,
        AnsweringRequest.model_fields["as_of"].annotation,
        request.answer_brief is answer_brief,
        request.answer_brief,
        request.as_of,
        "as_of" not in AnswerBrief.model_fields,
        "context" not in AnsweringRequest.model_fields,
    ) == (
        {"answer_brief", "as_of"},
        AnswerBrief,
        datetime,
        True,
        answer_brief,
        as_of,
        True,
        True,
    )
