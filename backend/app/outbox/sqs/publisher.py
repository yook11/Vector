from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from uuid import UUID

from botocore.client import BaseClient
from botocore.session import Session

from app.outbox.publishing.errors import (
    PublishError,
    PublishPhase,
)
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.publishing.route import EventMessage
from app.outbox.sqs.batch_response import results_from_sqs_batch_response
from app.outbox.sqs.client import create_sqs_client
from app.outbox.sqs.error_mapping import (
    publish_cleanup_error_from_exception,
    publish_error_from_exception,
)
from app.outbox.sqs.failure_handler import SqsPublishFailureHandler
from app.outbox.sqs.message_batch import SqsMessageBatch
from app.outbox.sqs.response_errors import InvalidSqsBatchResponse


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


class SqsSender:
    """生成済みメッセージを、資格情報を確定した単一試行で送信する。"""

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
        self, *, queue_url: str, messages: Sequence[EventMessage]
    ) -> BatchPublishResult:
        successes: list[PublishSucceeded] = []
        failures: list[PublishFailed] = []
        if not isinstance(queue_url, str) or not queue_url.strip():
            raise ValueError("queue URL must not be blank")
        batch = SqsMessageBatch(messages)
        failures.extend(batch.failures)
        if batch.messages:
            try:
                client = self._client_factory()
            except Exception as exc:
                error = publish_error_from_exception(exc, phase=PublishPhase.INITIALIZE)
                failures.extend(failed_results(batch, error=error).values())
            else:
                try:
                    sent_results = self._send_messages(client, batch, queue_url)
                finally:
                    try:
                        client.close()
                    except Exception as exc:
                        try:
                            error = publish_cleanup_error_from_exception(exc)
                            self._failure_handler.handle_cleanup_failure(error)
                        except Exception:  # noqa: S110 — 終了診断の障害で元の結果を変更しない。
                            pass
                for outcome in sent_results.values():
                    if isinstance(outcome, PublishSucceeded):
                        successes.append(outcome)
                    else:
                        failures.append(outcome)
        results_by_event_id = {
            outcome.event_id: outcome for outcome in (*successes, *failures)
        }
        return BatchPublishResult(
            results=tuple(
                results_by_event_id[envelope.event_id] for envelope in batch.inputs
            ),
        )

    def _send_messages(
        self, client: BaseClient, batch: SqsMessageBatch, queue_url: str
    ) -> Mapping[UUID, PublishSucceeded | PublishFailed]:
        """送信全体の失敗と応答内の個別結果を、それぞれの対象に反映する。"""
        try:
            response = client.send_message_batch(
                QueueUrl=queue_url,
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
                self._failure_handler.handle_invalid_response(
                    error=exc,
                    event_ids=tuple(message.event_id for message in batch.messages),
                )
            error = publish_error_from_exception(exc, phase=PublishPhase.SEND)
            return failed_results(batch, error=error)
