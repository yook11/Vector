"""送信準備と、本文とMD5の対応を SqsMessage の契約として検証する。"""

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.outbox.publishing.route import EventMessage
from app.outbox.sqs.message import SqsMessage


@pytest.fixture
def event():
    return EventMessage(UUID(int=1), "本文")


def test_sqs_message_hides_body_and_preserves_immutable_fields(event):
    """本文と照合情報を保持する型が、表示や後からの書き換えで漏洩しない。"""
    body = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    message = SqsMessage.from_message(EventMessage(event.event_id, body))
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
    message = SqsMessage.from_message(EventMessage(event.event_id, body))
    assert message.body_md5 == expected_md5
    with pytest.raises(TypeError):
        SqsMessage(event_id=event.event_id, body=body, body_md5=expected_md5)
