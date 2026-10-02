"""Direct Answer工程エラーの契約テスト。"""

from __future__ import annotations

import pytest

from app.agent.answering.direct_answer.failure import DirectAnswerError


def test_direct_answer_error_keeps_non_empty_code() -> None:
    error = DirectAnswerError(code="ai_provider_transport_error")

    assert error.code == "ai_provider_transport_error"
    assert str(error) == "ai_provider_transport_error"


def test_direct_answer_error_rejects_empty_code() -> None:
    with pytest.raises(ValueError, match="code must not be empty"):
        DirectAnswerError(code="")
