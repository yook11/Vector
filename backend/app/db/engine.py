"""用途別のEngine生成入口と共通の組み立て規則。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

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
# 別 audit session を開く経路 (acquisition の変換棄却) があり飽和不可能の
# 保証ではない。二重 audit 分は max_overflow + pool_timeout fail-fast で吸収する。
WORKER_POOL_SIZING: dict[str, tuple[int, int]] = {
    "dispatch": (5, 5),
    "collection": (5, 5),
    "trend_discovery": (2, 2),
    "briefing": (5, 5),
    "agent": (5, 5),
}
# Neon autosuspend (既定 300s) の手前で接続を張り替え、pre_ping 依存を
# 減らす (60s マージン)。共通既定 (3600) を worker のみ override する。
WORKER_POOL_RECYCLE_SECONDS = 240
DEFAULT_POOL_RECYCLE = 3600
DEFAULT_POOL_TIMEOUT = 5


class _RuntimeDatabaseSettings(Protocol):
    """通常アプリの用途別Engine生成に必要な設定。"""

    database_url: str
    db_iam_auth: bool
    aws_region: str | None


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


def create_lambda_engine(settings: _RuntimeDatabaseSettings) -> AsyncEngine:
    """呼び出し間でイベントループや接続を共有しないLambda用Engineを作る。"""
    return _create_engine(
        settings.database_url,
        application_name="vector-outbox-relay",
        password_provider=_runtime_password_provider(settings, settings.database_url),
        poolclass=NullPool,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def create_source_dispatch_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """予定回の対象選定用Engineを、IAM認証と呼び出し単位の接続で作る。"""
    if not settings.db_iam_auth:
        raise ValueError("Source dispatch requires RDS IAM authentication")
    if not callable(password_provider):
        raise TypeError("Source dispatch requires an IAM password provider")
    return _create_engine(
        settings.database_url,
        application_name="vector-source-dispatch",
        password_provider=password_provider,
        poolclass=NullPool,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def create_article_fetch_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    application_name: str,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """外部取得の待機中にDB接続を残さない記事取得工程用Engineを作る。"""
    if not settings.db_iam_auth:
        raise ValueError("Consumer requires RDS IAM authentication")
    if not callable(password_provider):
        raise TypeError("Consumer requires an IAM password provider")
    return _create_engine(
        settings.database_url,
        application_name=application_name,
        password_provider=password_provider,
        poolclass=NullPool,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def create_embedding_consumer_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """Consumerの利用範囲内で1接続だけ再利用するEngineを作る。"""
    if not settings.db_iam_auth:
        raise ValueError("Consumer requires RDS IAM authentication")
    if not callable(password_provider):
        raise TypeError("Consumer requires an IAM password provider")
    return _create_engine(
        settings.database_url,
        application_name="vector-embedding-consumer",
        password_provider=password_provider,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def create_curation_consumer_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """Consumerの利用範囲内で1接続だけ再利用するEngineを作る。"""
    if not settings.db_iam_auth:
        raise ValueError("Consumer requires RDS IAM authentication")
    if not callable(password_provider):
        raise TypeError("Consumer requires an IAM password provider")
    return _create_engine(
        settings.database_url,
        application_name="vector-curation-consumer",
        password_provider=password_provider,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def create_assessment_consumer_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """Consumerの利用範囲内で1接続だけ再利用するEngineを作る。"""
    if not settings.db_iam_auth:
        raise ValueError("Consumer requires RDS IAM authentication")
    if not callable(password_provider):
        raise TypeError("Consumer requires an IAM password provider")
    return _create_engine(
        settings.database_url,
        application_name="vector-assessment-consumer",
        password_provider=password_provider,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
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


type BackfillStage = Literal["curation", "assessment", "embedding"]


def create_backfill_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    stage: BackfillStage,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """一回のbackfill内で最大1接続を再利用するEngineを作る。"""
    if not settings.db_iam_auth:
        raise ValueError("Backfill requires RDS IAM authentication")
    if not callable(password_provider):
        raise TypeError("Backfill requires an IAM password provider")
    return _create_engine(
        settings.database_url,
        application_name=f"vector-backfill-{stage}",
        password_provider=password_provider,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def create_auth_rate_limit_cleanup_engine(
    settings: _RuntimeDatabaseSettings,
    *,
    password_provider: Callable[[], Awaitable[str]],
) -> AsyncEngine:
    """一回の削除に使う接続を、Lambda期限より短い待機上限で作る。"""
    return _create_engine(
        settings.database_url,
        application_name="vector-auth-rate-limit-cleanup",
        password_provider=password_provider,
        poolclass=NullPool,
        connect_args={
            "timeout": 5,
            "command_timeout": 15,
            "server_settings": {"statement_timeout": "15000", "lock_timeout": "3000"},
        },
    )
