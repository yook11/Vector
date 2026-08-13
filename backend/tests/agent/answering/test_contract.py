"""Shared answering contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.question_context.contract import QuestionContext


def test_answering_request_is_a_frozen_context_consumer_wrapper() -> None:
    context = QuestionContext(standalone_question="NVIDIA の直近発表は？")
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    request = AnsweringRequest(context=context, as_of=as_of)

    with pytest.raises(ValidationError):
        request.context = QuestionContext(standalone_question="別の質問")
    with pytest.raises(ValidationError):
        AnsweringRequest(context=context, as_of=as_of, previous_answer="前の回答")

    assert (
        set(AnsweringRequest.model_fields),
        AnsweringRequest.model_fields["context"].annotation,
        AnsweringRequest.model_fields["as_of"].annotation,
        request.context is context,
        request.context,
        request.as_of,
        "as_of" not in QuestionContext.model_fields,
    ) == (
        {"context", "as_of"},
        QuestionContext,
        datetime,
        True,
        context,
        as_of,
        True,
    )
