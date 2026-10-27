"""呼び出し開始時の期限を使い、認証カウンターを一度だけ削除する。"""

import asyncio
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager

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
from app.log_policy.policies.auth_rate_limit_cleanup import (
    AUTH_RATE_LIMIT_CLEANUP_LOG_RULES,
)
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, AUTH_RATE_LIMIT_CLEANUP_LOG_RULES)
RETENTION_MS = 10 * 60 * 1000


class AuthRateLimitCleanupFailed(RuntimeError):
    """Lambdaの失敗通知へ接続情報やSQL例外を持ち出さない。"""


def _record_failure(operation: str, error: Exception, *, started: float) -> None:
    logger.error(
        "auth_rate_limit_cleanup_failed",
        operation=operation,
        requires_investigation=True,
        duration_ms=round((time.monotonic() - started) * 1000),
        exc_info=error,
    )


async def run_cleanup(
    settings: AuthRateLimitCleanupSettings,
    *,
    cutoff_ms: int,
    started: float,
) -> int:
    """IAM署名器とDB接続を呼び出し単位で所有し、commit後に解放する。"""
    operation = "rds_client"
    try:
        async with AsyncExitStack() as resources:
            rds = Session().create_client(
                "rds",
                region_name=settings.aws_region,
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )
            resources.callback(rds.close)
            operation = "engine"
            password_provider = build_iam_password_provider(
                settings.database_url,
                region=settings.aws_region,
                generate_token=rds.generate_db_auth_token,
            )
            engine = create_auth_rate_limit_cleanup_engine(
                settings, password_provider=password_provider
            )
            resources.push_async_callback(engine.dispose)
            operation = "connect"
            async with engine.begin() as connection:
                operation = "delete"
                deleted = await delete_expired_counters(connection, cutoff_ms)
                operation = "commit"
            operation = "cleanup"
        return deleted
    except Exception as exc:
        _record_failure(operation, exc, started=started)
        raise AuthRateLimitCleanupFailed("auth rate limit cleanup failed") from None


@contextmanager
def _open_invocation(context: object) -> Iterator[None]:
    """呼び出しの相関情報をcontextvarsに置き、呼び出し元の文脈は終了時に戻す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        structlog.contextvars.bind_contextvars(service="auth")
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            structlog.contextvars.bind_contextvars(request_id=request_id)
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


def handler(event: object, context: object) -> None:
    """入力に依存せず、成功時だけ削除件数を記録する。"""
    cutoff_ms = time.time_ns() // 1_000_000 - RETENTION_MS
    started = time.monotonic()
    with _open_invocation(context):
        try:
            settings = AuthRateLimitCleanupSettings()  # type: ignore[call-arg]
        except Exception as exc:
            _record_failure("settings", exc, started=started)
            raise AuthRateLimitCleanupFailed("auth rate limit cleanup failed") from None
        structlog.contextvars.bind_contextvars(environment=settings.env)
        deleted = asyncio.run(
            run_cleanup(settings, cutoff_ms=cutoff_ms, started=started)
        )
        logger.info(
            "auth_rate_limit_cleanup_completed",
            deleted=deleted,
            cutoff_ms=cutoff_ms,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
