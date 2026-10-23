"""外部依存なしで、送信入力の型・ID・順序・不変性を検証する。"""

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.outbox.publishing.message_batch import MessageBatch
from app.outbox.publishing.route import EventMessage


@pytest.mark.parametrize("count", [1, 10, 11])
def test_batch_preserves_messages_without_sqs_count_limit(count):
    """SQSの件数制限に依存せず、入力の本文と順序をそのまま保持する。"""
    messages = [EventMessage(UUID(int=count - i), "{}") for i in range(count)]

    batch = MessageBatch(messages)

    assert batch.messages == tuple(messages)


def test_batch_copies_input_sequence():
    """元のリストを変更しても、構築済みのバッチは変化しない。"""
    message = EventMessage(UUID(int=1), "{}")
    messages = [message]
    batch = MessageBatch(messages)

    messages.clear()

    assert batch.messages == (message,)


@pytest.mark.parametrize("messages", [None, "private", b"private", iter(())])
def test_non_sequence_messages_are_rejected(messages):
    """メッセージのSequenceとして扱えない入力は拒否する。"""
    with pytest.raises(TypeError, match="sequence of EventMessage"):
        MessageBatch(messages)


def test_invalid_message_is_rejected():
    """EventMessage以外の要素を含む入力は拒否する。"""
    with pytest.raises(TypeError, match="invalid EventMessage"):
        MessageBatch([object()])


def test_empty_batch_is_rejected():
    """送信対象がないバッチは構築できない。"""
    with pytest.raises(ValueError, match="must not be empty"):
        MessageBatch([])


def test_duplicate_event_ids_are_rejected():
    """本文が異なっていてもイベントIDが重複する入力は拒否する。"""
    event_id = UUID(int=1)
    with pytest.raises(ValueError, match="IDs must be unique"):
        MessageBatch([EventMessage(event_id, "{}"), EventMessage(event_id, "[]")])


def test_batch_is_immutable():
    """構築後のメッセージ集合を書き換えられない。"""
    batch = MessageBatch([EventMessage(UUID(int=1), "{}")])
    with pytest.raises(FrozenInstanceError):
        batch.messages = ()
