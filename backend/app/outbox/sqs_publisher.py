from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC

from botocore.client import BaseClient
from botocore.session import Session

from app.analysis.assessment.events import ArticleAssessedInScope
from app.outbox.publish_errors import (
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishPhase,
    PublishUnexpectedError,
)
from app.outbox.publisher import EventEnvelope
from app.outbox.sqs_client import create_sqs_client
from app.outbox.sqs_error_mapping import publish_error_from_sqs_exception


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

    def publish(self, envelope: EventEnvelope) -> None:
        phase = PublishPhase.PREPARE_EVENT
        try:
            body = self._message_body(envelope)
            phase = PublishPhase.INITIALIZE
            client = self._client_factory()
            phase = PublishPhase.SEND
            self._send(client, body)
        except PublishError:
            raise
        except Exception as exc:
            raise PublishUnexpectedError(original_exception=exc, phase=phase) from exc

    def _send(self, client: BaseClient, body: str) -> None:
        failed = True
        try:
            try:
                client.send_message(
                    QueueUrl=self._embedding_queue_url, MessageBody=body
                )
            except PublishError:
                raise
            except Exception as exc:
                try:
                    failure = publish_error_from_sqs_exception(exc)
                except Exception as classification_exc:
                    raise PublishUnexpectedError(
                        original_exception=exc,
                        phase=PublishPhase.CLASSIFY_FAILURE,
                        classification_exception=classification_exc,
                    ) from exc
                if failure is None:
                    failure = PublishUnexpectedError(
                        original_exception=exc, phase=PublishPhase.SEND
                    )
                raise failure from exc
            failed = False
        finally:
            try:
                client.close()
            except PublishError:
                if not failed:
                    raise
            except Exception as exc:
                # 終了処理の失敗で、送信失敗やキャンセルを上書きしない。
                if not failed:
                    raise PublishUnexpectedError(
                        original_exception=exc, phase=PublishPhase.CLEANUP
                    ) from exc

    @staticmethod
    def _message_body(envelope: EventEnvelope) -> str:
        if envelope.event_type != ArticleAssessedInScope.EVENT_TYPE:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
            )
        if envelope.occurred_at.utcoffset() is None:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.INVALID_OCCURRED_AT
            )
        occurred_at = envelope.occurred_at.astimezone(UTC).isoformat()
        body = {
            "event_id": str(envelope.event_id),
            "event_type": envelope.event_type,
            "schema_version": envelope.schema_version,
            "occurred_at": occurred_at.replace("+00:00", "Z"),
            "payload": envelope.payload,
        }
        try:
            return json.dumps(body, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise PublishEventInvalidError(
                reason=PublishEventInvalidReason.SERIALIZATION_FAILED
            ) from exc
