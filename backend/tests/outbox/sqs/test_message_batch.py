"""工程に依存しない本文の入力・サイズ・不変性と、送れないイベントの切り分けを検証する。"""

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.outbox.publishing.errors import PublishEventInvalidReason
from app.outbox.publishing.route import EventMessage
from app.outbox.sqs.message_batch import SqsMessageBatch


def message(index=1, body="{}"):
    return EventMessage(UUID(int=index), body)


@pytest.mark.parametrize("count", [1, 10])
def test_batch_preserves_input_order_and_contents(count):
    """生成済み本文を変更せず、送信可能な件数と入力順を維持する。"""
    source = [message(count - i) for i in range(count)]
    batch = SqsMessageBatch(source)
    assert [(m.event_id, m.body) for m in batch.sendable] == [
        (m.event_id, m.body) for m in source
    ]
    assert batch.outgoing is not None
    assert batch.outgoing.entries() == [
        {"Id": str(m.event_id), "MessageBody": m.body} for m in source
    ]
    source.clear()
    assert len(batch.inputs) == count
    assert batch.failures == ()


@pytest.mark.parametrize(
    "source,error",
    [
        (None, TypeError),
        ("private", TypeError),
        ([object()], TypeError),
        ([], ValueError),
        ([message()] * 2, ValueError),
        ([message(i) for i in range(11)], ValueError),
    ],
)
def test_input_contract_is_checked_before_preparation(source, error):
    """型・件数・ID重複の契約違反を本文の送信準備前に拒否する。"""
    with pytest.raises(error):
        SqsMessageBatch(source)


@pytest.mark.parametrize("extra", [0, 1])
def test_combined_serialized_size_boundary(monkeypatch, extra):
    """送信できる本文合計の上限をUTF-8バイト数で確認する。"""
    source = [message(1), message(2)]
    monkeypatch.setattr("app.aws.sqs.outgoing_message.MAX_MESSAGE_BYTES", 4 - extra)
    if extra:
        with pytest.raises(ValueError, match="size limit"):
            SqsMessageBatch(source)
    else:
        assert len(SqsMessageBatch(source).sendable) == 2


def test_oversized_message_is_excluded_without_losing_valid_message(monkeypatch):
    """個別サイズ超過だけをイベント不正の失敗にし、送信可能な本文を残す。"""
    monkeypatch.setattr("app.aws.sqs.outgoing_message.MAX_MESSAGE_BYTES", 2)
    batch = SqsMessageBatch([message(1, "long"), message(2)])
    assert [m.event_id for m in batch.sendable] == [UUID(int=2)]
    assert [(f.event_id, f.error.reason) for f in batch.failures] == [
        (UUID(int=1), PublishEventInvalidReason.MESSAGE_TOO_LARGE)
    ]


def test_all_oversized_messages_leave_nothing_to_send(monkeypatch):
    """送れる本文が1件もなければ、SQSの送信単位を作らない。"""
    monkeypatch.setattr("app.aws.sqs.outgoing_message.MAX_MESSAGE_BYTES", 2)
    batch = SqsMessageBatch([message(1, "long")])
    assert (batch.sendable, batch.outgoing) == ((), None)


def test_batch_is_immutable():
    """構築後のバッチを書き換えられない。"""
    batch = SqsMessageBatch([message()])
    with pytest.raises(FrozenInstanceError):
        batch.sendable = ()
