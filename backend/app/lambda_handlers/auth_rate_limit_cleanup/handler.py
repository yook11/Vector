"""呼び出し開始時の期限を使い、認証カウンターを一度だけ削除する。"""

import asyncio
import time
from contextlib import AsyncExitStack

import structlog
from botocore.config import Config
from botocore.session import Session

from app.db.engine import create_auth_rate_limit_cleanup_engine
from app.db.iam import build_iam_password_provider
from app.lambda_handlers.auth_rate_limit_cleanup.repository import (
    delete_expired_counters,
)
from app.lambda_handlers.auth_rate_limit_cleanup.settings import (
    AuthRateLimitCleanupSettings,
)
from app.lambda_handlers.logging import setup_lambda_logging

logger = structlog.get_logger(__name__)
RETENTION_MS = 10 * 60 * 1000


class AuthRateLimitCleanupFailed(RuntimeError):
    """Lambdaの失敗通知へ接続情報やSQL例外を持ち出さない。"""


def _record_failure(phase: str, error: Exception, request_id: str | None) -> None:
    logger.error(
        "auth_rate_limit_cleanup_failed",
        phase=phase,
        error_type=type(error).__name__,
        request_id=request_id,
    )


async def run_cleanup(
    settings: AuthRateLimitCleanupSettings,
    *,
    cutoff_ms: int,
    request_id: str | None,
) -> int:
    """IAM署名器とDB接続を呼び出し単位で所有し、commit後に解放する。"""
    phase = "rds_client"
    try:
        async with AsyncExitStack() as resources:
            rds = Session().create_client(
                "rds",
                region_name=settings.aws_region,
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )
            resources.callback(rds.close)
            phase = "engine"
            password_provider = build_iam_password_provider(
                settings.database_url,
                region=settings.aws_region,
                generate_token=rds.generate_db_auth_token,
            )
            engine = create_auth_rate_limit_cleanup_engine(
                settings, password_provider=password_provider
            )
            resources.push_async_callback(engine.dispose)
            phase = "connect"
            async with engine.begin() as connection:
                phase = "delete"
                deleted = await delete_expired_counters(connection, cutoff_ms)
                phase = "commit"
            phase = "cleanup"
        return deleted
    except Exception as exc:
        _record_failure(phase, exc, request_id)
        raise AuthRateLimitCleanupFailed("auth rate limit cleanup failed") from None


def handler(event: object, context: object) -> None:
    """入力に依存せず、成功時だけ削除件数を記録する。"""
    cutoff_ms = time.time_ns() // 1_000_000 - RETENTION_MS
    started = time.monotonic()
    request_id = getattr(context, "aws_request_id", None)
    setup_lambda_logging()
    try:
        settings = AuthRateLimitCleanupSettings()  # type: ignore[call-arg]
    except Exception as exc:
        _record_failure("settings", exc, request_id)
        raise AuthRateLimitCleanupFailed("auth rate limit cleanup failed") from None
    deleted = asyncio.run(
        run_cleanup(settings, cutoff_ms=cutoff_ms, request_id=request_id)
    )
    logger.info(
        "auth_rate_limit_cleanup_completed",
        request_id=request_id,
        deleted=deleted,
        cutoff_ms=cutoff_ms,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
