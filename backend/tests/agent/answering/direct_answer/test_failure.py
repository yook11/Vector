"""Direct Answer工程エラーの契約テスト。"""

from __future__ import annotations

import pytest

from app.agent.answering.direct_answer.failure import DirectAnswerError


def test_direct_answer_error_keeps_non_empty_code() -> None:
    error = DirectAnswerError(code="ai_error_network")

    assert error.code == "ai_error_network"
    assert str(error) == "ai_error_network"


def test_direct_answer_error_rejects_empty_code() -> None:
    with pytest.raises(ValueError, match="code must not be empty"):
        DirectAnswerError(code="")
