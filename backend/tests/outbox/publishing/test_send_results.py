"""DB・通信を使わず、Senderの結果と送信対象の対応を検証する。"""

from uuid import UUID

import pytest

from app.outbox.publishing.contracts import (
    BatchPublishResult,
    PublishSucceeded,
    validate_send_results,
)
from app.outbox.publishing.route import EventMessage


@pytest.mark.parametrize(
    "ids",
    [[], [1], [1, 2, 3], [1, 3], [1, 1], [2, 1]],
    ids=["empty", "missing", "extra", "unknown", "duplicate", "reversed"],
)
def test_sender_results_must_match_message_order(ids):
    """結果の欠落・余分なID・未知ID・重複・順序違反を拒否する。"""
    messages = (EventMessage(UUID(int=1), "one"), EventMessage(UUID(int=2), "two"))
    result = BatchPublishResult(tuple(PublishSucceeded(UUID(int=i)) for i in ids))
    with pytest.raises(ValueError):
        validate_send_results(result, messages)


def test_sender_result_type_is_required():
    """Senderの戻り値が結果型でなければ拒否する。"""
    with pytest.raises(TypeError):
        validate_send_results(None, (EventMessage(UUID(int=1), "body"),))


def test_matching_results_are_accepted():
    """送信対象と結果が同じ順序で対応していれば受理する。"""
    messages = (EventMessage(UUID(int=1), "one"), EventMessage(UUID(int=2), "two"))
    result = BatchPublishResult(tuple(PublishSucceeded(m.event_id) for m in messages))
    validate_send_results(result, messages)
