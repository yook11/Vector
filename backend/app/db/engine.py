"""runtime が使う Engine の目録。

SSL と IAM を含む物理接続を組み立て、用途別の pool 設定を適用する。
Alembic は ``app.db.migration.engine`` が独立した設定境界を持つ。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db.connection import create_app_engine
from app.db.iam import build_iam_password_provider

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
# 減らす (60s マージン)。create_app_engine の factory 既定 (3600) を worker
# のみ override する (API は 3600 据え置き)。
WORKER_POOL_RECYCLE_SECONDS = 240
AUTH_RETENTION_POOL_SIZE = 1
AUTH_RETENTION_MAX_OVERFLOW = 1


def _create_runtime_engine(url: str, **engine_kwargs: Any) -> AsyncEngine:
    """解決済み URL に runtime の IAM と共通接続設定を適用する。"""
    engine_kwargs.setdefault("echo", False)
    if not settings.db_iam_auth:
        return create_app_engine(url, **engine_kwargs)
    region = settings.aws_region
    if region is None:
        raise RuntimeError("AWS_REGION is required when DB_IAM_AUTH is enabled")
    provider = build_iam_password_provider(url, region=region)
    return create_app_engine(url, password_provider=provider, **engine_kwargs)


def create_runtime_engine(**engine_kwargs: Any) -> AsyncEngine:
    """通常アプリ用の接続設定で runtime Engine を作る。"""
    return _create_runtime_engine(settings.database_url, **engine_kwargs)


def build_api_engine() -> AsyncEngine:
    """FastAPI 専用 engine を作る。最大同時接続は pool_size + max_overflow = 20。"""
    return create_runtime_engine(
        application_name=API_SERVICE_NAME,
        pool_size=API_POOL_SIZE,
        max_overflow=API_POOL_MAX_OVERFLOW,
    )


def worker_service_name(label: str) -> str:
    """worker プロセスの service 名 (= asyncpg application_name) を返す。"""
    return f"vector-worker-{label}"


def build_worker_engine(label: str) -> AsyncEngine:
    """``label`` の sizing で worker engine を作る。

    resilience (pre_ping / pool_timeout) は ``create_app_engine`` の既定に任せ、
    recycle のみ worker 値で override する。
    """
    pool_size, max_overflow = WORKER_POOL_SIZING[label]
    return create_runtime_engine(
        application_name=worker_service_name(label),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )


def build_cli_engine(application_name: str) -> AsyncEngine:
    """app 接続の CLI 用 engine を作る。pool は factory 既定のまま。"""
    return create_runtime_engine(application_name=application_name)


def auth_retention_service_name() -> str:
    """auth schema retention 用 DB 接続の service 名を返す。"""
    return "vector-worker-maintenance-auth"


def build_auth_retention_engine() -> AsyncEngine:
    """auth schema retention 用 engine を作る。

    通常 worker の ``database_url`` は vector_app role で auth."rateLimit" を
    触れないため、auth 保守用の接続文字列を別設定から受ける。
    """
    if settings.auth_retention_database_url is None:
        raise RuntimeError("AUTH_RETENTION_DATABASE_URL is not configured")
    return _create_runtime_engine(
        settings.auth_retention_database_url,
        application_name=auth_retention_service_name(),
        pool_size=AUTH_RETENTION_POOL_SIZE,
        max_overflow=AUTH_RETENTION_MAX_OVERFLOW,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )
