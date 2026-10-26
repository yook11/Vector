"""送れるイベントの組を受け取りSQSへまとめて送る実装が、組の対象と結果を対応付けて返すことを検証する(SQSクライアントはモック)。"""

import asyncio
import threading
from hashlib import md5
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.aws.sqs.message_sender import open_sqs_message_sender
from app.outbox.event.message import EventMessage
from app.outbox.relay.sender import MessageAccepted, MessageServiceError

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-embedding"


def _accepted(message):
    return {
        "Id": str(message.event_id),
        "MessageId": f"sqs-{message.event_id}",
        "MD5OfMessageBody": md5(
            message.body.encode("utf-8"), usedforsecurity=False
        ).hexdigest(),
    }


@pytest.mark.asyncio
async def test_results_are_paired_with_targets_in_given_order():
    """応答の順によらず、渡した組の対象と、そのメッセージの受付か失敗を渡した順で組にして返す。"""
    first = EventMessage(UUID(int=1), '{"n": 1}')
    second = EventMessage(UUID(int=2), '{"n": 2}')
    third = EventMessage(UUID(int=3), '{"n": 3}')
    client = Mock()
    client.send_message_batch.return_value = {
        "Successful": [_accepted(third), _accepted(first)],
        "Failed": [
            {"Id": str(second.event_id), "Code": "AccessDenied", "SenderFault": True}
        ],
    }
    targets = [object(), object(), object()]

    async with open_sqs_message_sender(
        client_factory=lambda: client, queue_url=QUEUE_URL
    ) as sender:
        send_results = await sender.send_batch(
            [(targets[0], first), (targets[1], second), (targets[2], third)]
        )

    assert [target for target, _ in send_results] == targets
    assert (send_results[0][1], send_results[2][1]) == (
        MessageAccepted(f"sqs-{first.event_id}"),
        MessageAccepted(f"sqs-{third.event_id}"),
    )
    assert (type(send_results[1][1]), send_results[1][1].reason) == (
        MessageServiceError,
        "access_denied",
    )
    client.send_message_batch.assert_called_once_with(
        QueueUrl=QUEUE_URL,
        Entries=[
            {"Id": str(first.event_id), "MessageBody": first.body},
            {"Id": str(second.event_id), "MessageBody": second.body},
            {"Id": str(third.event_id), "MessageBody": third.body},
        ],
    )


@pytest.mark.asyncio
async def test_duplicate_event_ids_are_rejected_before_sending():
    """同じイベントIDの本文を辞書で上書きせず、送信前に重複を拒否する。"""
    client = Mock()
    first = EventMessage(UUID(int=1), "{}")
    duplicate = EventMessage(UUID(int=1), "[]")
    async with open_sqs_message_sender(
        client_factory=lambda: client, queue_url="queue"
    ) as sender:
        with pytest.raises(ValueError, match="event IDs must be unique"):
            await sender.send_batch([(object(), first), (object(), duplicate)])
    client.send_message_batch.assert_not_called()


@pytest.mark.asyncio
async def test_input_mutation_during_send_preserves_original_targets():
    """通信待ちの間に入力のリストが変わっても、送った本文と元の対象の対応を保つ。"""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    target = object()
    message = EventMessage(UUID(int=1), "{}")
    sendable = [(target, message)]

    def send_message_batch(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return {"Successful": [_accepted(message)]}

    client = Mock(send_message_batch=send_message_batch)
    async with open_sqs_message_sender(
        client_factory=lambda: client, queue_url="queue"
    ) as sender:
        task = asyncio.create_task(sender.send_batch(sendable))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            sendable.clear()
        finally:
            release.set()
            results = await asyncio.wait_for(task, timeout=5)
    assert results == [(target, MessageAccepted(f"sqs-{message.event_id}"))]
