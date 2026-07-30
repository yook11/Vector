"""RDS IAM 認証: app runtime の接続に、接続ごとの auth token を差し込む。

password を持たない接続文字列 (AWS) では認証情報を接続のたびに IAM から作る。
token は 15 分で失効するので engine 生成時に 1 度作るのでは足りず、asyncpg の
callable password (接続確立ごとに呼ばれ、awaitable なら await される) に載せる。

``generate_db_auth_token`` は SigV4 のローカル署名で I/O を持たない。一方 credential
の取得・更新 (ECS の credential endpoint への HTTP) は同期 botocore が行うため、
``asyncio.to_thread`` に逃がして event loop を塞がない。

aiobotocore は採らない。botocore を狭い範囲に pin して更新を律速する一方、得られる
非同期化はこの用途 (ローカル署名 + 接続ごとの低頻度) では to_thread と変わらない。
AWS API を非同期で多数呼ぶ要件が出た時点で見直す。

region と credential は botocore の解決規則に任せる (ECS では task role と AWS_REGION)。

**射程は app runtime の接続だけ。** migration と運用スクリプトは ``create_app_engine``
を直接使い、migrator role の password 認証を続ける。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import botocore.session
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db_ssl import create_app_engine

# token は host:port ごとに署名するため、URL に port が無い場合の既定が要る。
_DEFAULT_POSTGRES_PORT = 5432


def build_iam_password_provider(url: str) -> Callable[[], Awaitable[str]]:
    """接続文字列の endpoint / user に対する auth token を返す provider を作る。

    error message に URL を載せない (password が混じる経路を作らない)。
    """
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
    port = parsed.port or _DEFAULT_POSTGRES_PORT
    client = botocore.session.get_session().create_client("rds")

    async def provide() -> str:
        return await asyncio.to_thread(
            client.generate_db_auth_token,
            DBHostname=host,
            Port=port,
            DBUsername=user,
        )

    return provide


def create_runtime_engine(url: str, **engine_kwargs: Any) -> AsyncEngine:
    """app runtime の engine を作る。IAM 認証が有効なら token provider を差し込む。

    無効時は ``create_app_engine`` と同じで、URL の password がそのまま使われる。
    migration / 運用スクリプトはこの関数を使わない (上の射程の注記)。
    """
    provider = build_iam_password_provider(url) if settings.db_iam_auth else None
    return create_app_engine(url, password_provider=provider, **engine_kwargs)
