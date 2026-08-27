"""Shared answering contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.threads.contracts import ThreadMessageSnapshot


def test_answering_request_is_a_frozen_question_and_history_wrapper() -> None:
    question = "NVIDIA の直近発表は？"
    history = (ThreadMessageSnapshot(role="user", content="前の質問"),)
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    request = AnsweringRequest(question=question, history=history, as_of=as_of)

    with pytest.raises(ValidationError):
        request.question = "別の質問"
    with pytest.raises(ValidationError):
        AnsweringRequest(question=question, as_of=as_of, previous_answer="前の回答")
    with pytest.raises(ValidationError):
        AnsweringRequest(answer_brief=question, as_of=as_of)

    assert (
        set(AnsweringRequest.model_fields),
        AnsweringRequest.model_fields["as_of"].annotation,
        request.question,
        request.history,
        request.as_of,
        "answer_brief" not in AnsweringRequest.model_fields,
    ) == (
        {"question", "history", "as_of"},
        datetime,
        question,
        history,
        as_of,
        True,
    )
