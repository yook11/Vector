"""記事単位AI分析の呼び出しに必要な資源を準備し、逆順に解放する。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Protocol

from botocore.config import Config
from botocore.session import Session
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.aws.ssm import get_secret_parameter
from app.db.iam import build_iam_password_provider
from app.db.session import caller_managed_session_factory

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
type IamPasswordProvider = Callable[[], Awaitable[str]]


class ArticleAnalysisLifecycleRecorder(Protocol):
    def record_initialization_failure(self, stage: str, error: Exception) -> None: ...

    def record_cleanup_failure(self, resource: str, error: Exception) -> None: ...


class EngineFactory(Protocol):
    def __call__(self, *, password_provider: IamPasswordProvider) -> AsyncEngine: ...


class ClientFactory[ClientT](Protocol):
    def __call__(
        self, *, api_key: SecretStr
    ) -> AbstractAsyncContextManager[ClientT]: ...


class ConsumerFactory[ConsumerT, ClientT](Protocol):
    def __call__(
        self, *, session_factory: SessionFactory, client: ClientT
    ) -> ConsumerT: ...


def _close_rds(
    close: Callable[[], None], recorder: ArticleAnalysisLifecycleRecorder
) -> None:
    try:
        close()
    except Exception as exc:
        recorder.record_cleanup_failure("rds", exc)


async def _dispose_engine(
    dispose: Callable[[], Awaitable[None]], recorder: ArticleAnalysisLifecycleRecorder
) -> None:
    try:
        await dispose()
    except Exception as exc:
        recorder.record_cleanup_failure("engine", exc)


@asynccontextmanager
async def open_article_analysis_consumer[ConsumerT, ClientT](
    *,
    aws_region: str,
    database_url: str,
    api_key_parameter_path: str,
    create_engine: EngineFactory,
    open_client: ClientFactory[ClientT],
    build_consumer: ConsumerFactory[ConsumerT, ClientT],
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> AsyncIterator[ConsumerT]:
    """準備済みConsumerを借用させ、利用終了後に所有資源を解放する。"""
    async with AsyncExitStack() as stack:
        stage = "resources"
        try:
            api_key = await asyncio.to_thread(
                get_secret_parameter, region=aws_region, path=api_key_parameter_path
            )
            rds = Session().create_client(
                "rds",
                region_name=aws_region,
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )
            stack.callback(_close_rds, rds.close, failure_recorder)
            password_provider = build_iam_password_provider(
                database_url,
                region=aws_region,
                generate_token=rds.generate_db_auth_token,
            )
            engine = create_engine(password_provider=password_provider)
            stack.push_async_callback(_dispose_engine, engine.dispose, failure_recorder)
            session_factory = caller_managed_session_factory(engine)

            stage = "ai_client"
            client = await stack.enter_async_context(open_client(api_key=api_key))
            stage = "consumer"
            consumer = build_consumer(session_factory=session_factory, client=client)
        except Exception as exc:
            failure_recorder.record_initialization_failure(stage, exc)
            raise

        yield consumer
