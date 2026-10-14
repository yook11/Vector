"""SDKへメッセージ本文を渡し、送信応答と通信資源を管理する。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from botocore.client import BaseClient
from botocore.config import Config
from botocore.session import Session

from app.aws.sqs.errors import (
    MessageSendError,
    SqsSendFailureKind,
    to_message_send_error,
)
from app.aws.sqs.outgoing_message import OutgoingSqsMessage
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger


class SqsClient(Protocol):
    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object: ...

    def close(self) -> None: ...


def create_sqs_client(*, session: Session, region: str) -> BaseClient:
    """呼び出し側の設定と資格情報providerで、送信用クライアントを作る。"""
    return session.create_client(
        "sqs",
        region_name=region,
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 3},
            proxies={},
            ignore_configured_endpoint_urls=True,
        ),
    )


class SqsMessageSender:
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
        task = asyncio.create_task(asyncio.to_thread(self._send, body))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 通信中のクライアントをcloseせず、通信終了後にキャンセルを伝える。
            try:
                await task
            except Exception:  # noqa: S110 — 通信失敗より外部キャンセルを優先する。
                pass
            raise


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
        raise MessageSendError(SqsSendFailureKind.CONFIGURATION)
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
