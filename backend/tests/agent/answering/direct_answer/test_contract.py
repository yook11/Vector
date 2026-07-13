"""Direct answer contract tests."""

import inspect
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerer,
    DirectAnswerGenerator,
)
from app.agent.answering.direct_answer.flow import DirectAnswerFlow


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_direct_answer_boundaries_accept_request_and_separate_previous_answer() -> None:
    assert (
        tuple(inspect.signature(DirectAnswerGenerator.stream).parameters),
        tuple(inspect.signature(DirectAnswerer.answer).parameters),
        tuple(inspect.signature(DirectAnswerFlow.answer).parameters),
        _first_input_annotation(DirectAnswerGenerator.stream),
        _first_input_annotation(DirectAnswerer.answer),
        _first_input_annotation(DirectAnswerFlow.answer),
    ) == (
        ("self", "request", "previous_answer", "previous_error"),
        ("self", "request", "previous_answer"),
        ("self", "request", "previous_answer"),
        AnsweringRequest,
        AnsweringRequest,
        AnsweringRequest,
    )


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_direct_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        DirectAnswerDraft(answer=answer)
