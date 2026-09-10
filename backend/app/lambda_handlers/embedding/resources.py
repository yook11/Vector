"""Consumer用の秘密情報とDB接続を利用範囲ごとに組み立てる。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import structlog
from botocore.config import Config
from botocore.session import Session
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.aws.ssm import get_secret_parameter
from app.db.engine import create_embedding_consumer_engine
from app.db.iam import build_iam_password_provider
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.embedding.failure_handler import EmbeddingLambdaFailureHandler
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingResources:
    """呼び出しの終了まで借用できる秘密情報とセッション生成器。"""

    gemini_api_key: SecretStr = field(repr=False)
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _close_sdk(close: Callable[[], None]) -> None:
    try:
        close()
    except Exception as exc:
        EmbeddingLambdaFailureHandler(logger).handle_cleanup_failure("rds", exc)


async def _dispose_engine(dispose: Callable[[], Awaitable[None]]) -> None:
    try:
        await dispose()
    except Exception as exc:
        EmbeddingLambdaFailureHandler(logger).handle_cleanup_failure("engine", exc)


@asynccontextmanager
async def open_embedding_resources(
    settings: EmbeddingConsumerSettings,
) -> AsyncIterator[EmbeddingResources]:
    """各記事のセッション終了後、利用範囲を抜けると全資源を閉じる。"""
    api_key = await asyncio.to_thread(
        get_secret_parameter,
        region=settings.aws_region,
        path=settings.gemini_api_key_parameter_path,
    )
    async with AsyncExitStack() as stack:
        password_provider = None
        if settings.db_iam_auth:
            rds = Session().create_client(
                "rds",
                region_name=settings.aws_region,
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )
            stack.callback(_close_sdk, rds.close)
            password_provider = build_iam_password_provider(
                settings.database_url,
                region=settings.aws_region,
                generate_token=rds.generate_db_auth_token,
            )
        engine = create_embedding_consumer_engine(
            settings, password_provider=password_provider
        )
        stack.push_async_callback(_dispose_engine, engine.dispose)
        yield EmbeddingResources(
            gemini_api_key=api_key,
            session_factory=caller_managed_session_factory(engine),
        )
