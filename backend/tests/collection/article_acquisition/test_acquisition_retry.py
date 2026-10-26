"""取得依頼の送信の失敗を送り直すかの判断。SQSへ送らず、変換後の失敗を直接渡す。"""

from __future__ import annotations

import pytest

from app.collection.article_acquisition.acquisition_retry import (
    should_retry_acquisition_send,
)
from app.outbox.relay.sender import (
    MessageSendError,
    MessageServiceError,
    MessageServiceErrorReason,
)


@pytest.mark.parametrize(
    ("error", "attempt", "expected"),
    [
        (
            MessageServiceError(
                reason=MessageServiceErrorReason.THROTTLED,
                service_code="RequestThrottled",
            ),
            2,
            True,
        ),
        (
            MessageServiceError(
                reason=MessageServiceErrorReason.THROTTLED,
                service_code="RequestThrottled",
            ),
            3,
            False,
        ),
        (
            MessageServiceError(
                reason=MessageServiceErrorReason.ACCESS_DENIED,
                service_code="AccessDenied",
            ),
            1,
            False,
        ),
    ],
)
def test_resend_only_resolvable_failure_until_third_attempt(
    error: MessageSendError, attempt: int, expected: bool
) -> None:
    """送り直せば直りうる失敗は2回目の失敗までは送り直して3回目では諦め、直らない失敗は1回目でも送り直さない。"""
    assert should_retry_acquisition_send(error, attempt=attempt) is expected
