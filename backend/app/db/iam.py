"""設定層に依存しない RDS IAM auth token provider。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

import botocore.session
from sqlalchemy.engine import make_url

_DEFAULT_POSTGRES_PORT = 5432


@lru_cache(maxsize=1)
def _rds_client(region: str) -> Any:
    """credential更新を共有するRDS clientをプロセス内で再利用する。"""
    return botocore.session.get_session().create_client("rds", region_name=region)


def build_iam_password_provider(
    url: str,
    *,
    region: str,
    token_port: int | None = None,
) -> Callable[[], Awaitable[str]]:
    """URLのendpoint/userに対する接続時token providerを作る。"""
    parsed = make_url(url)
    host = parsed.host
    if host is None:
        raise ValueError("an RDS auth token requires a host in the connection string")
    user = parsed.username
    if user is None:
        raise ValueError(
            "an RDS auth token requires a user in the connection string "
            "(the token is signed per database user)"
        )
    port = (
        token_port if token_port is not None else parsed.port or _DEFAULT_POSTGRES_PORT
    )
    if not 1 <= port <= 65535:
        raise ValueError("RDS IAM auth token port must be between 1 and 65535")

    def generate() -> str:
        return _rds_client(region).generate_db_auth_token(
            DBHostname=host,
            Port=port,
            DBUsername=user,
        )

    async def provide() -> str:
        return await asyncio.to_thread(generate)

    return provide
