"""外部から記事を取得する工程の起動とDB資源の解放を担う。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Protocol

from botocore.config import Config
from botocore.session import Session
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn
from app.db.iam import build_iam_password_provider
from app.db.session import caller_managed_session_factory
from app.http.settings import HttpSettings

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
type IamPasswordProvider = Callable[[], Awaitable[str]]


class EngineFactory(Protocol):
    def __call__(self, *, password_provider: IamPasswordProvider) -> AsyncEngine: ...


class ConsumerFactory[ConsumerT](Protocol):
    def __call__(self, *, session_factory: SessionFactory) -> ConsumerT: ...


class ArticleFetchLifecycleRecorder:
    """工程名を添えた診断だけを出し、通常の出力障害を利用側へ返さない。"""

    def __init__(self, logger: BoundLogger, *, operation: str) -> None:
        self._logger = logger
        self._operation = operation

    def record_initialization_failure(self, stage: str, error: Exception) -> None:
        self._record(
            "initialization_failed", stage=stage, error_class=exception_fqn(error)
        )

    def record_cleanup_failure(self, resource: str, error: Exception) -> None:
        self._record(
            "resources_cleanup_failed",
            resource=resource,
            error_class=exception_fqn(error),
        )

    def _record(self, event: str, **fields: object) -> None:
        try:
            self._logger.warning(f"{self._operation}_{event}", **fields)
        except Exception:  # noqa: S110
            # 診断の失敗で元の処理結果を変更しない。
            pass


def _close_rds(
    close: Callable[[], None], recorder: ArticleFetchLifecycleRecorder
) -> None:
    try:
        close()
    except Exception as exc:
        recorder.record_cleanup_failure("rds", exc)


async def _dispose_engine(
    dispose: Callable[[], Awaitable[None]], recorder: ArticleFetchLifecycleRecorder
) -> None:
    try:
        await dispose()
    except Exception as exc:
        recorder.record_cleanup_failure("engine", exc)


@asynccontextmanager
async def open_article_fetch_consumer[ConsumerT](
    *,
    aws_region: str,
    database_url: str,
    create_engine: EngineFactory,
    build_consumer: ConsumerFactory[ConsumerT],
    failure_recorder: ArticleFetchLifecycleRecorder,
) -> AsyncIterator[ConsumerT]:
    """HTTPの前提を検証し、呼び出し専用のDB資源で工程を組み立てる。"""
    async with AsyncExitStack() as stack:
        stage = "http_settings"
        try:
            HttpSettings()
            stage = "resources"
            rds = Session().create_client(
                "rds",
                region_name=aws_region,
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )
            stack.callback(_close_rds, rds.close, failure_recorder)
            provider = build_iam_password_provider(
                database_url,
                region=aws_region,
                generate_token=rds.generate_db_auth_token,
            )
            engine = create_engine(password_provider=provider)
            stack.push_async_callback(_dispose_engine, engine.dispose, failure_recorder)
            session_factory = caller_managed_session_factory(engine)
            stage = "consumer"
            consumer = build_consumer(session_factory=session_factory)
        except Exception as exc:
            failure_recorder.record_initialization_failure(stage, exc)
            raise
        yield consumer
