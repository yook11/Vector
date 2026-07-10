"""Direct answer contract tests."""

import pytest
from pydantic import ValidationError

from app.agent.answering.direct_answer.contract import DirectAnswerDraft


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_direct_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        DirectAnswerDraft(answer=answer)
