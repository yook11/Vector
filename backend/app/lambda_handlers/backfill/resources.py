"""backfill呼び出し単位で接続を所有し、元の失敗を保って解放する。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from botocore.config import Config
from botocore.session import Session

from app.aws.sqs.message_sender import create_sqs_client, open_sqs_message_sender
from app.db.engine import BackfillStage, create_backfill_engine
from app.db.iam import build_iam_password_provider
from app.db.session import SessionFactory, caller_managed_session_factory
from app.lambda_handlers.backfill.failure_recorder import record_failure
from app.lambda_handlers.backfill.settings import BackfillConnectionSettings
from app.outbox.relay.sender import MessageSenderFactory


@dataclass(frozen=True)
class BackfillResources:
    session_factory: SessionFactory
    sender_factory: MessageSenderFactory


@asynccontextmanager
async def open_backfill_resources(
    settings: BackfillConnectionSettings,
    *,
    stage: BackfillStage,
    queue_url: str,
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
            sqs_session = Session()
            resources = BackfillResources(
                session_factory,
                lambda: open_sqs_message_sender(
                    client_factory=lambda: create_sqs_client(
                        session=sqs_session,
                        region=settings.aws_region,
                        max_attempts=1,
                    ),
                    queue_url=queue_url,
                ),
            )
        except BaseException as exc:
            record_failure(stage, phase, exc)
            raise

        yield resources
    # 呼び出し側が失敗した場合はここへ到達せず、先行した例外が伝わる。
    if cleanup_errors:
        raise cleanup_errors[0]
