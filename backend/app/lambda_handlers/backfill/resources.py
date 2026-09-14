"""backfill呼び出し単位で接続を所有し、元の失敗を保って解放する。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from botocore.config import Config
from botocore.session import Session

from app.db.engine import BackfillStage, create_backfill_engine
from app.db.iam import build_iam_password_provider
from app.db.session import SessionFactory, caller_managed_session_factory
from app.lambda_handlers.backfill.failure_recorder import record_failure
from app.lambda_handlers.backfill.settings import BackfillConnectionSettings
from app.outbox.publishing.publisher import EventPublisher
from app.outbox.publishing.route import EventDeliveryRoute
from app.outbox.publishing.routed_publisher import RoutedEventPublisher
from app.outbox.sqs.publisher import SqsSender


@dataclass(frozen=True)
class BackfillResources:
    session_factory: SessionFactory
    publisher: EventPublisher


@asynccontextmanager
async def open_backfill_resources(
    settings: BackfillConnectionSettings,
    *,
    stage: BackfillStage,
    route: EventDeliveryRoute,
) -> AsyncIterator[BackfillResources]:
    cleanup_errors: list[BaseException] = []
    async with AsyncExitStack() as stack:
        phase = "rds"
        try:
            rds = Session().create_client(
                "rds",
                region_name=settings.aws_region,
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )

            def close_rds() -> None:
                try:
                    rds.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                    record_failure(stage, "rds_cleanup", exc)

            stack.callback(close_rds)
            phase = "engine"
            password_provider = build_iam_password_provider(
                settings.database_url,
                region=settings.aws_region,
                generate_token=rds.generate_db_auth_token,
            )
            engine = create_backfill_engine(
                settings, stage=stage, password_provider=password_provider
            )

            async def dispose_engine() -> None:
                try:
                    await engine.dispose()
                except BaseException as exc:
                    cleanup_errors.append(exc)
                    record_failure(stage, "engine_cleanup", exc)

            stack.push_async_callback(dispose_engine)
            session_factory = caller_managed_session_factory(engine)
            phase = "publisher"
            sender = SqsSender.from_session(
                session=Session(), region=settings.aws_region
            )
            resources = BackfillResources(
                session_factory, RoutedEventPublisher(route, sender)
            )
        except BaseException as exc:
            record_failure(stage, phase, exc)
            raise

        yield resources
    # 呼び出し側が失敗した場合はここへ到達せず、先行した例外が伝わる。
    if cleanup_errors:
        raise cleanup_errors[0]
