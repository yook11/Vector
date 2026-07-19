"""Direct answer contract tests."""

import inspect
from dataclasses import FrozenInstanceError, fields
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerer,
    DirectAnswerInput,
)
from app.agent.answering.direct_answer.flow import DirectAnswerFlow


def _first_input_annotation(method: object) -> object | None:
    parameter_names = tuple(inspect.signature(method).parameters)
    return get_type_hints(method).get(parameter_names[1])


def test_direct_answer_boundaries_use_typed_attempt_input() -> None:
    assert (
        tuple(inspect.signature(DirectAnswerer.answer).parameters),
        tuple(inspect.signature(DirectAnswerFlow.answer).parameters),
        _first_input_annotation(DirectAnswerer.answer),
        _first_input_annotation(DirectAnswerFlow.answer),
    ) == (
        ("self", "request", "previous_answer"),
        ("self", "request", "previous_answer"),
        AnsweringRequest,
        AnsweringRequest,
    )


def test_direct_answer_input_is_frozen_and_keeps_attempt_state_together() -> None:
    assert [field.name for field in fields(DirectAnswerInput)] == [
        "request",
        "previous_answer",
        "previous_error",
    ]
    input = DirectAnswerInput(
        request=object(),  # type: ignore[arg-type]
        previous_answer="previous",
    )
    assert input.previous_error is None
    with pytest.raises(FrozenInstanceError):
        input.previous_answer = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_direct_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        DirectAnswerDraft(answer=answer)
