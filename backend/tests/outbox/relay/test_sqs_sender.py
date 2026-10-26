"""送れるイベントの組を受け取りSQSへまとめて送る実装が、組の対象と結果を対応付けて返すことを検証する(SQSクライアントはモック)。"""

from hashlib import md5
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.aws.sqs.errors import SqsServiceError
from app.aws.sqs.outgoing_message import AcceptedSqsMessage
from app.outbox.event.message import EventMessage
from app.outbox.relay.sqs_sender import open_sqs_event_message_sender

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

    async with open_sqs_event_message_sender(
        client_factory=lambda: client, queue_url=QUEUE_URL
    ) as sender:
        send_results = await sender.send_batch(
            [(targets[0], first), (targets[1], second), (targets[2], third)]
        )

    assert [target for target, _ in send_results] == targets
    assert (send_results[0][1], send_results[2][1]) == (
        AcceptedSqsMessage(f"sqs-{first.event_id}"),
        AcceptedSqsMessage(f"sqs-{third.event_id}"),
    )
    assert (type(send_results[1][1]), send_results[1][1].reason) == (
        SqsServiceError,
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
