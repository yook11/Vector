"""Direct answer contract tests."""

from dataclasses import FrozenInstanceError, fields
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)


def test_direct_answer_input_is_frozen_and_keeps_attempt_state_together() -> None:
    assert [field.name for field in fields(DirectAnswerInput)] == [
        "request",
        "previous_answer",
        "previous_output_truncated",
    ]
    type_hints = get_type_hints(DirectAnswerInput)
    assert type_hints.get("previous_output_truncated") is bool
    input = DirectAnswerInput(
        request=object(),  # type: ignore[arg-type]
        previous_answer="previous",
    )
    assert input.previous_output_truncated is False
    with pytest.raises(FrozenInstanceError):
        input.previous_answer = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_direct_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        DirectAnswerDraft(answer=answer)
