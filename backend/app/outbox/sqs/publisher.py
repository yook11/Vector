from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from uuid import UUID

import structlog
from botocore.client import BaseClient
from botocore.session import Session

from app.outbox.publishing.errors import (
    PublishError,
    PublishPhase,
)
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.sqs.batch_response import results_from_sqs_batch_response
from app.outbox.sqs.client import create_sqs_client
from app.outbox.sqs.error_mapping import (
    publish_cleanup_error_from_exception,
    publish_error_from_exception,
)
from app.outbox.sqs.event_batch import EventBatch
from app.outbox.sqs.message_batch import SqsMessageBatch
from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

logger = structlog.get_logger(__name__)


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
        events = EventBatch(envelopes)
        batch = SqsMessageBatch(events)
        results: dict[UUID, PublishSucceeded | PublishFailed] = {
            failure.event_id: failure for failure in batch.failures
        }
        if batch.messages:
            results.update(self._send_batch(batch))
        return BatchPublishResult(
            results=tuple(results[envelope.event_id] for envelope in events.envelopes),
        )

    def _send_batch(
        self, batch: SqsMessageBatch
    ) -> Mapping[UUID, PublishSucceeded | PublishFailed]:
        try:
            client = self._client_factory()
        except Exception as exc:
            error = publish_error_from_exception(exc, phase=PublishPhase.INITIALIZE)
            return failed_results(batch, error=error)

        try:
            results = self._send_messages(client, batch)
        finally:
            try:
                client.close()
            except Exception as exc:
                self._record_cleanup_error(exc)
        return results

    @staticmethod
    def _record_cleanup_error(exc: Exception) -> None:
        """終了診断の通常失敗が、送信結果や先行例外を上書きしないようにする。"""
        try:
            error = publish_cleanup_error_from_exception(exc)
            logger.warning(
                "outbox_publish_cleanup_failed",
                error_code=error.CODE,
                original_exception_type=error.original_exception_type,
            )
        except Exception:  # noqa: S110 — 診断出力を再帰させず元の結果を維持する。
            pass

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
            if isinstance(exc, InvalidSqsBatchResponse):
                self._record_invalid_response(exc, batch=batch)
            error = publish_error_from_exception(exc, phase=PublishPhase.SEND)
            return failed_results(batch, error=error)

    @staticmethod
    def _record_invalid_response(
        error: InvalidSqsBatchResponse, *, batch: SqsMessageBatch
    ) -> None:
        """送信対象のIDと固定の診断項目だけを記録し、出力障害を配信へ戻さない。"""
        try:
            logger.warning(
                "outbox_sqs_response_invalid",
                error_reason=error.reason.value,
                response_field=error.field.value,
                event_ids=[str(message.event_id) for message in batch.messages],
            )
        except Exception:  # noqa: S110 — 診断出力を再帰させず送信結果を維持する。
            pass
