"""DB・通信を使わず、送信結果の値型が保証する構築条件を検証する。"""

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.analysis.assessment.events import (
    AssessedEventInvalidError,
)
from app.analysis.curation.events import (
    CuratedEventInvalidError,
)
from app.aws.sqs.errors import MessageSendUnexpectedError
from app.aws.sqs.outgoing_message import MessageTooLargeError
from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
)
from app.collection.events import (
    AnalyzableEventInvalidError,
)
from app.db.errors import DatabaseUnexpectedError
from app.outbox.publishing.contracts import (
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.publishing.route import EventRouteMismatchError
from app.shared.event_contract import (
    EventInvalid,
    EventInvalidReason,
)


def _send_failure():
    return MessageSendUnexpectedError(exception_type="RuntimeError")


@pytest.mark.parametrize("event_id", [None, 1, True, "private-event-id"])
@pytest.mark.parametrize("failed", [False, True])
def test_individual_result_rejects_non_uuid(event_id, failed):
    """成功・失敗ともにIDの暗黙変換をせず、不正値を文面へ出さない。"""
    with pytest.raises(TypeError, match="event_id must be a UUID") as caught:
        if failed:
            PublishFailed(event_id, _send_failure())
        else:
            PublishSucceeded(event_id)
    assert str(event_id) not in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        MessageTooLargeError(),
        AnalyzableEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        IncompleteArticleEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        AssessedEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        CuratedEventInvalidError(
            EventInvalid(reason=EventInvalidReason.INVALID_PAYLOAD, issues=())
        ),
        EventRouteMismatchError(),
    ],
)
def test_failed_result_accepts_every_publish_failure(error):
    """SQSへの送信の失敗、4つのイベントの構築失敗、outbox自身の種別の不一致を、イベントの送信失敗として持てる。"""
    assert PublishFailed(UUID(int=1), error).error is error


@pytest.mark.parametrize(
    "error",
    [
        None,
        RuntimeError("private-error"),
        DatabaseUnexpectedError(),
    ],
)
def test_failed_result_rejects_non_publish_failure(error):
    """通常の例外やDBの失敗を、イベントの送信失敗として構築できない。"""
    with pytest.raises(TypeError, match="must be a PublishFailure") as caught:
        PublishFailed(UUID(int=1), error)
    assert "private-error" not in str(caught.value)


def test_results_preserve_values():
    """送信結果の参照を維持し、結果のフィールドは変更できない。"""
    error = _send_failure()
    success = PublishSucceeded(UUID(int=1))
    failure = PublishFailed(UUID(int=2), error)
    assert failure.error is error
    for value, field in ((success, "event_id"), (failure, "error")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)
