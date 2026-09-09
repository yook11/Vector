"""送信準備と、本文とMD5の対応を SqsMessage の契約として検証する。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from hashlib import md5
from uuid import UUID

import pytest

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.sqs.message import SqsMessage


@pytest.fixture
def event():
    return ArticleAssessedInScopeEvent(
        event_id=UUID(int=1),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, 3, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )


def test_sqs_message_hides_body_and_preserves_immutable_fields(event):
    """本文と照合情報を保持する型が、表示や後からの書き換えで漏洩しない。"""
    body = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    message = SqsMessage(event_id=event.event_id, body=body)
    assert body not in repr(message)
    assert message.body_md5 not in repr(message)
    for name, value in (
        ("event_id", UUID(int=2)),
        ("body", "changed"),
        ("body_md5", "0" * 32),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(message, name, value)


@pytest.mark.parametrize(
    "body,expected_md5",
    [
        ("{}", "99914b932bd37a50b983c5e7c90ae93b"),
        (
            ' { "text": "日本語\\n\\"引用\\"" }\n',
            "9d37d07e76129464ee66174bdcaff86a",
        ),
    ],
)
def test_sqs_message_derives_md5_from_exact_body(event, body, expected_md5):
    """本文のUTF-8バイト列からMD5を計算し、独立した照合値を受け取らない。"""
    message = SqsMessage(event_id=event.event_id, body=body)
    assert message.body_md5 == expected_md5
    with pytest.raises(TypeError):
        SqsMessage(event_id=event.event_id, body=body, body_md5=expected_md5)


def test_from_event_builds_specified_body_and_md5(event):
    """検証済みイベントの5項目を既存の本文形式にし、そのUTF-8本文のMD5を持つ。"""
    message = SqsMessage.from_event(event)
    expected_body = (
        '{"event_id": "00000000-0000-0000-0000-000000000001", '
        '"event_type": "article.assessed_in_scope", '
        '"schema_version": 1, '
        '"occurred_at": "2026-09-07T03:00:00Z", '
        '"payload": {"curation_id": 123, "analyzed_article_id": 456}}'
    )
    assert message.event_id == event.event_id
    assert message.body == expected_body
    assert (
        message.body_md5
        == md5(expected_body.encode("utf-8"), usedforsecurity=False).hexdigest()
    )


def test_from_event_keeps_utc_instant_and_fractional_seconds(event):
    """タイムゾーン付き時刻の意味と小数秒を、UTCのZ表記で本文に残す。"""
    message = SqsMessage.from_event(
        event.model_copy(
            update={
                "occurred_at": datetime(
                    2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
                )
            }
        )
    )
    assert '"occurred_at": "2026-09-07T03:00:00.123456Z"' in message.body


def test_from_event_rejects_oversized_body(event, monkeypatch):
    """UTF-8本文が上限を超えるイベントは SqsMessage を返さず、既知の不正理由にする。"""
    limit = len(SqsMessage.from_event(event).body.encode("utf-8"))
    monkeypatch.setattr("app.outbox.sqs.message.MAX_MESSAGE_BYTES", limit)
    oversized = event.model_copy(
        update={
            "payload": event.payload.model_copy(
                update={"curation_id": event.payload.curation_id * 10}
            )
        }
    )
    assert SqsMessage.from_event(event).body
    with pytest.raises(PublishEventInvalidError) as caught:
        SqsMessage.from_event(oversized)
    assert caught.value.reason is PublishEventInvalidReason.MESSAGE_TOO_LARGE
