from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import UUID

from botocore.client import BaseClient
from botocore.session import Session

from app.aws.sqs.errors import MessageSendError, to_message_send_error
from app.aws.sqs.outgoing_message import (
    AcceptedSqsMessage,
    MessageTooLargeError,
    OutgoingSqsMessage,
    OutgoingSqsMessageBatch,
    SqsResponseInvalidError,
)
from app.outbox.publishing.contracts import (
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.publishing.message_batch import MessageBatch
from app.outbox.sqs.client import create_sqs_client
from app.outbox.sqs.failure_handler import SqsPublishFailureHandler


class SqsSender:
    """生成済みメッセージを、SDKの内部再試行のない単一試行で送信する。"""

    def __init__(
        self,
        *,
        client_factory: Callable[[], BaseClient],
        failure_handler: SqsPublishFailureHandler,
    ) -> None:
        self._client_factory = client_factory
        self._failure_handler = failure_handler

    @classmethod
    def from_session(
        cls,
        *,
        session: Session,
        region: str,
    ) -> SqsSender:
        """設定層のregionとSDKの資格情報providerを送信処理に接続する。"""
        return cls(
            client_factory=lambda: create_sqs_client(session=session, region=region),
            failure_handler=SqsPublishFailureHandler(),
        )

    def send_batch(
        self, *, queue_url: str, batch: MessageBatch
    ) -> tuple[PublishSucceeded | PublishFailed, ...]:
        if not isinstance(queue_url, str) or not queue_url.strip():
            raise ValueError("queue URL must not be blank")
        if not isinstance(batch, MessageBatch):
            raise TypeError("batch must be a MessageBatch")
        if len(batch.messages) > 10:
            raise ValueError("SQS batch must contain at most ten messages")

        results: dict[UUID, PublishSucceeded | PublishFailed] = {}
        sqs_messages: dict[str, OutgoingSqsMessage] = {}

        for message in batch.messages:
            try:
                sqs_message = OutgoingSqsMessage(message.body)
            except MessageTooLargeError as error:
                results[message.event_id] = PublishFailed(message.event_id, error)
            else:
                sqs_messages[str(message.event_id)] = sqs_message

        if sqs_messages:
            outgoing = OutgoingSqsMessageBatch(sqs_messages)
            outcomes = self._send_messages(queue_url=queue_url, outgoing=outgoing)
            for entry_id, outcome in outcomes.items():
                event_id = UUID(entry_id)
                if isinstance(outcome, AcceptedSqsMessage):
                    results[event_id] = PublishSucceeded(event_id)
                else:
                    results[event_id] = PublishFailed(event_id, outcome)

        return tuple(results[message.event_id] for message in batch.messages)

    def _send_messages(
        self,
        *,
        queue_url: str,
        outgoing: OutgoingSqsMessageBatch,
    ) -> Mapping[str, AcceptedSqsMessage | MessageSendError]:
        client = None
        try:
            try:
                client = self._client_factory()
                response = client.send_message_batch(
                    QueueUrl=queue_url, Entries=outgoing.entries()
                )
            except Exception as exc:
                error = to_message_send_error(exc)
                return {entry_id: error for entry_id in outgoing.messages}

            try:
                return outgoing.verify(response)
            except SqsResponseInvalidError as error:
                self._failure_handler.handle_invalid_response(
                    error=error,
                    event_ids=tuple(UUID(entry_id) for entry_id in outgoing.messages),
                )
                return {entry_id: error for entry_id in outgoing.messages}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    try:
                        self._failure_handler.handle_cleanup_failure(exc)
                    except Exception:  # noqa: S110 — 終了診断の障害で元の結果を変更しない。
                        pass
