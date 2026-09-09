"""送信準備と、本文とMD5の対応を SqsMessage の契約として検証する。"""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import md5
from uuid import UUID

import pytest

from app.outbox.publish_errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.publisher import EventEnvelope
from app.outbox.sqs_message import MAX_MESSAGE_BYTES, SqsMessage


@pytest.fixture
def envelope():
    return EventEnvelope(
        event_id=UUID(int=1),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, 3, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )


def test_sqs_message_hides_body_and_preserves_immutable_fields(envelope):
    """本文と照合情報を保持する型が、表示や後からの書き換えで漏洩しない。"""
    body = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    message = SqsMessage(event_id=envelope.event_id, body=body)
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
def test_sqs_message_derives_md5_from_exact_body(envelope, body, expected_md5):
    """本文のUTF-8バイト列からMD5を計算し、独立した照合値を受け取らない。"""
    message = SqsMessage(event_id=envelope.event_id, body=body)
    assert message.body_md5 == expected_md5
    with pytest.raises(TypeError):
        SqsMessage(event_id=envelope.event_id, body=body, body_md5=expected_md5)


def test_from_envelope_builds_specified_body_and_md5(envelope):
    """保存済みイベントの5項目を再生成せず、そのUTF-8本文のMD5を持つ。"""
    message = SqsMessage.from_envelope(envelope)
    expected_body = (
        '{"event_id": "00000000-0000-0000-0000-000000000001", '
        '"event_type": "article.assessed_in_scope", '
        '"schema_version": 1, '
        '"occurred_at": "2026-09-07T03:00:00Z", '
        '"payload": {"curation_id": 123, "analyzed_article_id": 456}}'
    )
    assert message.event_id == envelope.event_id
    assert message.body == expected_body
    assert (
        message.body_md5
        == md5(expected_body.encode("utf-8"), usedforsecurity=False).hexdigest()
    )


def test_from_envelope_keeps_utc_instant_and_fractional_seconds(envelope):
    """タイムゾーン付き時刻の意味と小数秒を、UTCのZ表記で本文に残す。"""
    message = SqsMessage.from_envelope(
        replace(
            envelope,
            occurred_at=datetime(
                2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
            ),
        )
    )
    assert '"occurred_at": "2026-09-07T03:00:00.123456Z"' in message.body


@pytest.mark.parametrize("event_type", ["article.acquired", "future.event"])
def test_from_envelope_preserves_event_type_without_routing(envelope, event_type):
    """送信先の対応可否に依存せず、イベント種別を本文に保持する。"""
    message = SqsMessage.from_envelope(replace(envelope, event_type=event_type))
    assert json.loads(message.body)["event_type"] == event_type


@pytest.mark.parametrize(
    "field,value,reason",
    [
        (
            "occurred_at",
            datetime(2026, 9, 7),
            PublishEventInvalidReason.INVALID_OCCURRED_AT,
        ),
        ("payload", {"bad": object()}, PublishEventInvalidReason.SERIALIZATION_FAILED),
        (
            "payload",
            {"bad": float("nan")},
            PublishEventInvalidReason.SERIALIZATION_FAILED,
        ),
        (
            "payload",
            {"bad": "x" * MAX_MESSAGE_BYTES},
            PublishEventInvalidReason.MESSAGE_TOO_LARGE,
        ),
    ],
)
def test_from_envelope_rejects_unsendable_events(envelope, field, value, reason):
    """送れないイベントは SqsMessage を返さず、既知の不正理由にする。"""
    with pytest.raises(PublishEventInvalidError) as caught:
        SqsMessage.from_envelope(replace(envelope, **{field: value}))
    assert caught.value.reason is reason


def test_from_envelope_measures_serialized_json_bytes(envelope):
    """文字列の長さではなく、ASCIIエスケープ後の本文バイト数で上限を見る。"""
    event = replace(envelope, payload={"text": "あ" * (MAX_MESSAGE_BYTES // 6)})
    escaped = "\\u3042" * (MAX_MESSAGE_BYTES // 6)
    assert len(escaped.encode("utf-8")) + 80 > MAX_MESSAGE_BYTES
    with pytest.raises(PublishEventInvalidError) as caught:
        SqsMessage.from_envelope(event)
    assert caught.value.reason is PublishEventInvalidReason.MESSAGE_TOO_LARGE
