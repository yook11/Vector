"""SQSの可視性変更に使うクライアントと、キャンセル時も通信を回収する操作を提供する。"""

import asyncio
from typing import Protocol, cast

from botocore.config import Config
from botocore.session import Session


class SqsMessageVisibilityClient(Protocol):
    def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> object: ...

    def close(self) -> None: ...


def create_sqs_visibility_client(
    *, session: Session, region: str
) -> SqsMessageVisibilityClient:
    client = session.create_client(
        "sqs",
        region_name=region,
        config=Config(
            proxies={},
            ignore_configured_endpoint_urls=True,
            connect_timeout=5,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )
    return cast(SqsMessageVisibilityClient, client)


async def change_message_visibility(
    client: SqsMessageVisibilityClient,
    *,
    queue_url: str,
    receipt_handle: str,
    seconds: int,
) -> None:
    operation = asyncio.create_task(
        asyncio.to_thread(
            client.change_message_visibility,
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=seconds,
        )
    )
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        # 実行中のSDK通信は中断できないため、重なるキャンセルからも回収を守る。
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not operation.cancelled():
            operation.exception()
        raise
