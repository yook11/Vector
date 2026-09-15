"""呼び出し専用のDB接続と遅延生成するSQS送信部品を組み立てる。"""

import asyncio
from asyncio import sleep
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from random import random

from botocore.client import BaseClient
from botocore.config import Config
from botocore.session import Session
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aws.sqs.message_sender import create_sqs_client
from app.collection.article_acquisition.acquisition_dispatch import (
    SourceAcquisitionDispatcher,
)
from app.collection.article_acquisition.sqs_acquisition_sender import (
    open_acquisition_sender,
)
from app.collection.sources.dispatch import SourceDispatchService
from app.db.engine import create_source_dispatch_engine
from app.db.iam import build_iam_password_provider
from app.lambda_handlers.source_dispatch.failure import record_cleanup_failure
from app.lambda_handlers.source_dispatch.settings import SourceDispatchSettings


def _create_rds_client(region: str) -> BaseClient:
    return Session().create_client(
        "rds",
        region_name=region,
        config=Config(proxies={}, ignore_configured_endpoint_urls=True),
    )


async def _close_rds(client: BaseClient) -> None:
    try:
        await asyncio.to_thread(client.close)
    except Exception as exc:
        record_cleanup_failure("rds", exc)


@asynccontextmanager
async def _open_rds_client(region: str) -> AsyncIterator[BaseClient]:
    task = asyncio.create_task(asyncio.to_thread(_create_rds_client, region))
    try:
        client = await asyncio.shield(task)
    except asyncio.CancelledError:
        # 初期化中のSDKクライアントを回収してからキャンセルを伝える。
        try:
            client = await task
        except Exception:  # noqa: S110 — 初期化失敗よりキャンセルを優先する。
            pass
        else:
            await _close_rds(client)
        raise
    try:
        yield client
    finally:
        await _close_rds(client)


@asynccontextmanager
async def open_source_dispatcher(
    settings: SourceDispatchSettings,
) -> AsyncIterator[SourceAcquisitionDispatcher]:
    async with _open_rds_client(settings.aws_region) as rds:
        provider = build_iam_password_provider(
            settings.database_url,
            region=settings.aws_region,
            generate_token=rds.generate_db_auth_token,
        )
        engine = create_source_dispatch_engine(settings, password_provider=provider)
        try:
            yield SourceAcquisitionDispatcher(
                dispatch_service=SourceDispatchService(
                    async_sessionmaker(engine, expire_on_commit=False)
                ),
                sender_factory=lambda: open_acquisition_sender(
                    client_factory=lambda: create_sqs_client(
                        session=Session(), region=settings.aws_region
                    ),
                    queue_url=settings.sqs_source_acquisition_queue_url,
                ),
                sleep=sleep,
                jitter=random,
            )
        finally:
            try:
                await engine.dispose()
            except Exception as exc:
                record_cleanup_failure("engine", exc)
