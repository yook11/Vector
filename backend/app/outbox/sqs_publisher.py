from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from botocore.client import BaseClient
from botocore.session import Session

from app.outbox.publish_errors import (
    PublishCleanupError,
    PublishError,
    PublishPhase,
)
from app.outbox.publisher import (
    BatchPublishResult,
    EventEnvelope,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.sqs_batch_response import results_from_sqs_batch_response
from app.outbox.sqs_client import create_sqs_client
from app.outbox.sqs_error_mapping import (
    publish_cleanup_error_from_exception,
    publish_error_from_exception,
)
from app.outbox.sqs_message import SqsMessage
from app.outbox.sqs_message_batch import MAX_BATCH_MESSAGES, SqsMessageBatch


def failed_results(
    batch: SqsMessageBatch,
    *,
    error: PublishError,
) -> dict[UUID, PublishFailed]:
    """指定された送信対象に、分類済みの失敗を結び付ける。"""
    return {
        message.event_id: PublishFailed(message.event_id, error)
        for message in batch.messages
    }


class SqsEventPublisher:
    """ベクトル生成向けイベントを、資格情報を確定した単一試行で送信する。"""

    def __init__(
        self,
        *,
        embedding_queue_url: str,
        client_factory: Callable[[], BaseClient],
    ) -> None:
        self._client_factory = client_factory
        self._embedding_queue_url = embedding_queue_url

    @classmethod
    def from_session(
        cls,
        *,
        session: Session,
        region: str,
        embedding_queue_url: str,
    ) -> SqsEventPublisher:
        """設定層のregionとSDKの資格情報providerを送信処理に接続する。"""
        return cls(
            embedding_queue_url=embedding_queue_url,
            client_factory=lambda: create_sqs_client(session=session, region=region),
        )

    def publish_batch(self, envelopes: Sequence[EventEnvelope]) -> BatchPublishResult:
        self._validate_batch(envelopes)
        envelopes = tuple(envelopes)
        results: dict[UUID, PublishSucceeded | PublishFailed] = {}
        messages: list[SqsMessage] = []
        for envelope in envelopes:
            try:
                message = SqsMessage.from_envelope(envelope)
            except Exception as exc:
                error = publish_error_from_exception(
                    exc, phase=PublishPhase.PREPARE_EVENT
                )
                results[envelope.event_id] = PublishFailed(envelope.event_id, error)
            else:
                messages.append(message)
        cleanup_error = None
        if messages:
            batch = SqsMessageBatch(messages=tuple(messages))
            sent_results, cleanup_error = self._send_batch(batch)
            results.update(sent_results)
        return BatchPublishResult(
            results=tuple(results[envelope.event_id] for envelope in envelopes),
            cleanup_error=cleanup_error,
        )

    @staticmethod
    def _validate_batch(envelopes: Sequence[EventEnvelope]) -> None:
        if not isinstance(envelopes, Sequence) or isinstance(envelopes, (str, bytes)):
            raise TypeError("envelopes must be a sequence of EventEnvelope")
        if not 1 <= len(envelopes) <= MAX_BATCH_MESSAGES:
            raise ValueError("batch must contain between one and ten events")
        for envelope in envelopes:
            if (
                not isinstance(envelope, EventEnvelope)
                or not isinstance(envelope.event_id, UUID)
                or not isinstance(envelope.event_type, str)
                or type(envelope.schema_version) is not int
                or not isinstance(envelope.occurred_at, datetime)
                or not isinstance(envelope.payload, dict)
            ):
                raise TypeError("invalid EventEnvelope field type")
        if len({envelope.event_id for envelope in envelopes}) != len(envelopes):
            raise ValueError("batch event IDs must be unique")

    def _send_batch(
        self, batch: SqsMessageBatch
    ) -> tuple[
        Mapping[UUID, PublishSucceeded | PublishFailed], PublishCleanupError | None
    ]:
        try:
            client = self._client_factory()
        except Exception as exc:
            error = publish_error_from_exception(exc, phase=PublishPhase.INITIALIZE)
            return failed_results(batch, error=error), None

        cleanup_error = None
        try:
            results = self._send_messages(client, batch)
        finally:
            try:
                client.close()
            except Exception as exc:
                cleanup_error = publish_cleanup_error_from_exception(exc)
        return results, cleanup_error

    def _send_messages(
        self, client: BaseClient, batch: SqsMessageBatch
    ) -> Mapping[UUID, PublishSucceeded | PublishFailed]:
        """送信全体の失敗と応答内の個別結果を、それぞれの対象に反映する。"""
        try:
            response = client.send_message_batch(
                QueueUrl=self._embedding_queue_url,
                Entries=[
                    {"Id": str(message.event_id), "MessageBody": message.body}
                    for message in batch.messages
                ],
            )
        except Exception as exc:
            error = publish_error_from_exception(exc, phase=PublishPhase.SEND)
            return failed_results(batch, error=error)

        try:
            return results_from_sqs_batch_response(response, batch=batch)
        except Exception as exc:
            error = publish_error_from_exception(exc, phase=PublishPhase.SEND)
            return failed_results(batch, error=error)
