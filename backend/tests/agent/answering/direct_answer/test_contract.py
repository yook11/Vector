"""Direct answer contract tests."""

from dataclasses import FrozenInstanceError, fields

import pytest
from pydantic import ValidationError

from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)


def test_direct_answer_input_is_frozen_with_request_and_previous_answer() -> None:
    """回答の入力は再試行の状態を持たない2項目の不変の値である。"""
    assert [field.name for field in fields(DirectAnswerInput)] == [
        "request",
        "previous_answer",
    ]
    input = DirectAnswerInput(
        request=object(),  # type: ignore[arg-type]
        previous_answer="previous",
    )
    with pytest.raises(FrozenInstanceError):
        input.previous_answer = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_direct_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        DirectAnswerDraft(answer=answer)
