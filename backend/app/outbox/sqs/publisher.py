from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import UUID

import structlog
from botocore.client import BaseClient
from botocore.session import Session
from pydantic import ValidationError

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
    assessed_event_invalid_reason,
)
from app.outbox.publishing.errors import (
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
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
from app.outbox.sqs.message import SqsMessage
from app.outbox.sqs.message_batch import MAX_BATCH_MESSAGES, SqsMessageBatch
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
        self._validate_batch(envelopes)
        envelopes = tuple(envelopes)
        results: dict[UUID, PublishSucceeded | PublishFailed] = {}
        messages: list[SqsMessage] = []
        for envelope in envelopes:
            try:
                if envelope.event_type != ArticleAssessedInScope.EVENT_TYPE:
                    raise PublishEventInvalidError(
                        reason=PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
                    )
                event = self._validate_assessed_in_scope_event(envelope)
                message = SqsMessage.from_event(event)
            except Exception as exc:
                error = publish_error_from_exception(
                    exc, phase=PublishPhase.PREPARE_EVENT
                )
                results[envelope.event_id] = PublishFailed(envelope.event_id, error)
            else:
                messages.append(message)
        if messages:
            batch = SqsMessageBatch(messages=tuple(messages))
            results.update(self._send_batch(batch))
        return BatchPublishResult(
            results=tuple(results[envelope.event_id] for envelope in envelopes),
        )

    @staticmethod
    def _validate_assessed_in_scope_event(
        envelope: EventEnvelope,
    ) -> ArticleAssessedInScopeEvent:
        """対象内判定イベントを検証し、型付きpayloadとともに返す。"""
        try:
            event = ArticleAssessedInScopeEvent(
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                schema_version=envelope.schema_version,
                occurred_at=envelope.occurred_at,
                payload=envelope.payload,
            )
        except ValidationError as exc:
            reason = PublishEventInvalidReason(assessed_event_invalid_reason(exc))
        else:
            return event
        raise PublishEventInvalidError(reason=reason)

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
