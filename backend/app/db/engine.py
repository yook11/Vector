"""用途別のEngine生成入口と共通の組み立て規則。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.iam import build_iam_password_provider
from app.db.migration.settings import MigrationSettings
from app.db.ssl import split_ssl_from_url

API_SERVICE_NAME = "vector-api"
API_POOL_SIZE = 10
API_POOL_MAX_OVERFLOW = 10

# worker engine の pool sizing (label -> (pool_size, max_overflow))。
# 均一既定 (5,5)=cap10。trend_discovery のみ日次 cron・fan-out なし・
# 最大 1 connection のため縮小 (2,2)=cap4。
# supervisord の --max-async-tasks は該当 worker の cap 以下に保つ
# (通常パスの上限ガード、tests/test_brokers.py が pin する)。error-path で
# 別 audit session を開く経路 (acquisition の変換棄却 / curation の
# ready-build 失敗) があり飽和不可能の保証ではない。二重 audit 分は
# max_overflow + pool_timeout fail-fast で吸収する。
WORKER_POOL_SIZING: dict[str, tuple[int, int]] = {
    "dispatch": (5, 5),
    "collection": (5, 5),
    "analysis": (5, 5),
    "embedding": (5, 5),
    "trend_discovery": (2, 2),
    "briefing": (5, 5),
    "agent": (5, 5),
    "maintenance": (5, 5),
}
# Neon autosuspend (既定 300s) の手前で接続を張り替え、pre_ping 依存を
# 減らす (60s マージン)。共通既定 (3600) を worker のみ override する。
WORKER_POOL_RECYCLE_SECONDS = 240
AUTH_RETENTION_POOL_SIZE = 1
AUTH_RETENTION_MAX_OVERFLOW = 1
DEFAULT_POOL_RECYCLE = 3600
DEFAULT_POOL_TIMEOUT = 5


class _RuntimeDatabaseSettings(Protocol):
    """通常アプリの用途別Engine生成に必要な設定。"""

    database_url: str
    db_iam_auth: bool
    aws_region: str | None
    auth_retention_database_url: str | None


def _merge_server_settings(
    connect_args: dict[str, Any], application_name: str | None
) -> dict[str, Any]:
    """application_nameをasyncpgのserver_settingsへ注入する。"""
    if application_name is None:
        return connect_args
    server_settings = {
        **connect_args.get("server_settings", {}),
        "application_name": application_name,
    }
    return {**connect_args, "server_settings": server_settings}


def _create_engine(
    url: str,
    *,
    application_name: str | None = None,
    password_provider: Callable[[], Awaitable[str]] | None = None,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """SSL・認証・利用者名・pool既定値を適用してEngineを組み立てる。"""
    clean_url, ssl_connect_args = split_ssl_from_url(url)
    caller_connect_args = dict(engine_kwargs.pop("connect_args", {}))
    if "ssl" in caller_connect_args:
        raise ValueError(
            "connect_args['ssl'] must not be passed to an Engine factory; "
            "SSL is derived from the connection string's sslmode (single source "
            "of truth). Use `?sslmode=require` instead."
        )
    if "password" in caller_connect_args:
        raise ValueError(
            "connect_args['password'] must not be passed to an Engine factory; "
            "use the IAM settings, or keep the password in the connection string."
        )
    merged_connect_args = {**caller_connect_args, **ssl_connect_args}
    merged_connect_args = _merge_server_settings(merged_connect_args, application_name)
    if password_provider is not None:
        merged_connect_args["password"] = password_provider

    engine_kwargs.setdefault("pool_pre_ping", True)
    engine_kwargs.setdefault("pool_recycle", DEFAULT_POOL_RECYCLE)
    engine_kwargs.setdefault("hide_parameters", True)
    if engine_kwargs.get("poolclass") is not NullPool:
        engine_kwargs.setdefault("pool_timeout", DEFAULT_POOL_TIMEOUT)
    return create_async_engine(
        clean_url, connect_args=merged_connect_args, **engine_kwargs
    )


def _runtime_password_provider(
    settings: _RuntimeDatabaseSettings, url: str
) -> Callable[[], Awaitable[str]] | None:
    """通常アプリ設定から接続ごとのIAM token providerを作る。"""
    if not settings.db_iam_auth:
        return None
    if settings.aws_region is None:
        raise RuntimeError("AWS_REGION is required when DB_IAM_AUTH is enabled")
    return build_iam_password_provider(url, region=settings.aws_region)


def create_api_engine(settings: _RuntimeDatabaseSettings) -> AsyncEngine:
    """FastAPI専用Engineを作る。最大同時接続は20。"""
    return _create_engine(
        settings.database_url,
        application_name=API_SERVICE_NAME,
        password_provider=_runtime_password_provider(settings, settings.database_url),
        echo=False,
        pool_size=API_POOL_SIZE,
        max_overflow=API_POOL_MAX_OVERFLOW,
    )


def worker_service_name(label: str) -> str:
    """workerプロセスのapplication_nameを返す。"""
    return f"vector-worker-{label}"


def create_worker_engine(settings: _RuntimeDatabaseSettings, label: str) -> AsyncEngine:
    """指定workerのpool設定を持つEngineを作る。"""
    pool_size, max_overflow = WORKER_POOL_SIZING[label]
    return _create_engine(
        settings.database_url,
        application_name=worker_service_name(label),
        password_provider=_runtime_password_provider(settings, settings.database_url),
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )


def create_cli_engine(
    settings: _RuntimeDatabaseSettings,
    application_name: str,
    *,
    database_url: str | None = None,
    use_configured_auth: bool = True,
) -> AsyncEngine:
    """CLI用途のEngineを作る。"""
    url = database_url or settings.database_url
    provider = (
        _runtime_password_provider(settings, url) if use_configured_auth else None
    )
    return _create_engine(
        url,
        application_name=application_name,
        password_provider=provider,
        echo=False,
    )


def auth_retention_service_name() -> str:
    """auth schema retention用DB接続のapplication_nameを返す。"""
    return "vector-worker-maintenance-auth"


def create_auth_retention_engine(
    settings: _RuntimeDatabaseSettings,
) -> AsyncEngine:
    """auth schema retention用Engineを作る。"""
    if settings.auth_retention_database_url is None:
        raise RuntimeError("AUTH_RETENTION_DATABASE_URL is not configured")
    url = settings.auth_retention_database_url
    return _create_engine(
        url,
        application_name=auth_retention_service_name(),
        password_provider=_runtime_password_provider(settings, url),
        echo=False,
        pool_size=AUTH_RETENTION_POOL_SIZE,
        max_overflow=AUTH_RETENTION_MAX_OVERFLOW,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )


def create_migration_engine(
    settings: MigrationSettings,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """migration専用設定からEngineを作る。"""
    provider = None
    if settings.db_iam_auth:
        if settings.aws_region is None:
            raise RuntimeError("AWS_REGION is required when DB_IAM_AUTH is enabled")
        provider = build_iam_password_provider(
            settings.migration_database_url,
            region=settings.aws_region,
            token_port=settings.rds_iam_auth_token_port,
        )
    return _create_engine(
        settings.migration_database_url,
        application_name="vector-migration",
        password_provider=provider,
        **engine_kwargs,
    )
