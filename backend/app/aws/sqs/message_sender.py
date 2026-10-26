"""SDKへメッセージ本文を渡し、送信応答と通信資源を管理する。"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID

from botocore.config import Config
from botocore.session import Session

from app.aws.sqs.errors import to_message_send_error
from app.aws.sqs.outgoing_message import (
    MessageTooLargeError,
    OutgoingSqsMessage,
    OutgoingSqsMessageBatch,
    SqsResponseInvalidError,
)
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.outbox.event.message import EventMessage
from app.outbox.relay.sender import (
    MessageAccepted,
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSender,
    MessageSendError,
)


class SqsClient(Protocol):
    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object: ...

    def send_message_batch(
        self, *, QueueUrl: str, Entries: list[dict[str, str]]
    ) -> object: ...

    def close(self) -> None: ...


def create_sqs_client(*, session: Session, region: str, max_attempts: int) -> SqsClient:
    """呼び出し側の設定と資格情報providerで、送信1回あたりの試行回数を指定して送信用クライアントを作る。"""
    client = session.create_client(
        "sqs",
        region_name=region,
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": max_attempts},
            proxies={},
            ignore_configured_endpoint_urls=True,
        ),
    )
    # botocoreはAPIの操作を実行時に生やすため、型の上ではSqsClientとして扱う。
    return cast(SqsClient, client)


async def _run_until_transport_ends[T](call: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 通信中のクライアントをcloseせず、通信終了後にキャンセルを伝える。
        try:
            await task
        except Exception:  # noqa: S110 — 通信失敗より外部キャンセルを優先する。
            pass
        raise


class SqsMessageSender(MessageSender):
    def __init__(self, *, client: SqsClient, queue_url: str) -> None:
        self._client = client
        self._queue_url = queue_url

    def _send(self, body: str) -> None:
        message = OutgoingSqsMessage(body)
        try:
            response = self._client.send_message(
                QueueUrl=self._queue_url, MessageBody=message.body
            )
        except Exception as exc:
            raise to_message_send_error(exc) from None
        message.verify(response)

    async def send(self, body: str) -> None:
        await _run_until_transport_ends(lambda: self._send(body))

    def _send_batch(
        self, bodies: Mapping[UUID, str]
    ) -> Mapping[UUID, MessageAccepted | MessageSendError]:
        results: dict[str, MessageAccepted | MessageSendError] = {}
        messages: dict[str, OutgoingSqsMessage] = {}
        for key, body in bodies.items():
            # SQSのentry IDは文字列で、UUIDの文字列表記は長さと文字の制約を満たす。
            entry_id = str(key)
            try:
                messages[entry_id] = OutgoingSqsMessage(body)
            except MessageTooLargeError as error:
                results[entry_id] = error
        if messages:
            batch = OutgoingSqsMessageBatch(messages)
            try:
                response = self._client.send_message_batch(
                    QueueUrl=self._queue_url, Entries=batch.entries()
                )
            except Exception as exc:
                results.update(dict.fromkeys(messages, to_message_send_error(exc)))
            else:
                try:
                    results.update(batch.verify(response))
                except SqsResponseInvalidError as error:
                    results.update(dict.fromkeys(messages, error))
        return MappingProxyType({key: results[str(key)] for key in bodies})

    async def send_batch[T](
        self, sendable: Sequence[tuple[T, EventMessage]]
    ) -> list[tuple[T, MessageAccepted | MessageSendError]]:
        """送信対象は解釈せず、対応する本文の受付か失敗と組にして入力順で返す。"""
        items = tuple(sendable)
        if not 1 <= len(items) <= 10:
            raise ValueError("batch must contain between one and ten messages")
        bodies = {message.event_id: message.body for _, message in items}
        if len(bodies) != len(items):
            raise ValueError("event IDs must be unique")
        results = await _run_until_transport_ends(lambda: self._send_batch(bodies))
        return [(target, results[message.event_id]) for target, message in items]


async def _close_client(client: SqsClient) -> None:
    try:
        await asyncio.to_thread(client.close)
    except Exception as exc:
        create_policy_json_logger(__name__, PIPELINE_CONTROL_LOG_RULES).warning(
            "sqs_send_client_cleanup_failed", exc_info=exc
        )


@asynccontextmanager
async def open_sqs_message_sender(
    *, client_factory: Callable[[], SqsClient], queue_url: str
) -> AsyncIterator[SqsMessageSender]:
    """1回の送信処理で使うクライアントを取得し、キャンセル時も回収する。"""
    if not queue_url.strip():
        raise MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.MISSING_QUEUE_URL
        )
    task = asyncio.create_task(asyncio.to_thread(client_factory))
    try:
        client = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            client = await task
        except Exception:  # noqa: S110 — 初期化失敗より外部キャンセルを優先する。
            pass
        else:
            await _close_client(client)
        raise
    except Exception as exc:
        raise to_message_send_error(exc) from None
    try:
        yield SqsMessageSender(client=client, queue_url=queue_url)
    finally:
        await _close_client(client)
