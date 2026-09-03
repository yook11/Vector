"""設定済みURLから物理接続用Engineを組み立てる。"""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.ssl import split_ssl_from_url

DEFAULT_POOL_RECYCLE = 3600
DEFAULT_POOL_TIMEOUT = 5


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


def create_app_engine(
    url: str,
    *,
    application_name: str | None = None,
    password_provider: Callable[[], Awaitable[str]] | None = None,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """SSL・認証・利用者名・pool既定値を適用するEngine生成入口。"""
    clean_url, ssl_connect_args = split_ssl_from_url(url)
    caller_connect_args = dict(engine_kwargs.pop("connect_args", {}))
    if "ssl" in caller_connect_args:
        raise ValueError(
            "connect_args['ssl'] must not be passed to create_app_engine; "
            "SSL is derived from the connection string's sslmode (single source "
            "of truth). Use `?sslmode=require` instead."
        )
    if "password" in caller_connect_args:
        raise ValueError(
            "connect_args['password'] must not be passed to create_app_engine; "
            "pass password_provider= for IAM auth, or keep the password in the "
            "connection string."
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
