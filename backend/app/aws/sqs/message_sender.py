"""SDKへメッセージ本文を渡し、送信応答と通信資源を管理する。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import structlog
from botocore.client import BaseClient
from botocore.config import Config
from botocore.session import Session

from app.aws.sqs.errors import (
    SqsSendError,
    SqsSendFailure,
    SqsSendFailureKind,
    classify_sqs_send_failure,
)
from app.aws.sqs.send_response import SqsSendResponse

logger = structlog.get_logger(__name__)


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
        try:
            raw_response = self._client.send_message(
                QueueUrl=self._queue_url, MessageBody=body
            )
        except Exception as exc:
            raise SqsSendError(classify_sqs_send_failure(exc)) from None
        response = SqsSendResponse.from_response(raw_response)
        response.verify_body(body)

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
        try:
            logger.warning(
                "sqs_send_client_cleanup_failed",
                exception_type=type(exc).__name__,
            )
        except Exception:  # noqa: S110 — 診断の失敗で送信結果を変更しない。
            pass


@asynccontextmanager
async def open_sqs_message_sender(
    *, client_factory: Callable[[], SqsClient], queue_url: str
) -> AsyncIterator[SqsMessageSender]:
    """1回の送信処理で使うクライアントを取得し、キャンセル時も回収する。"""
    if not queue_url.strip():
        raise SqsSendError(SqsSendFailure(SqsSendFailureKind.CONFIGURATION))
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
        raise SqsSendError(classify_sqs_send_failure(exc)) from None
    try:
        yield SqsMessageSender(client=client, queue_url=queue_url)
    finally:
        await _close_client(client)
