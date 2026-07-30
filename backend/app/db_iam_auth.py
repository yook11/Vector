"""RDS IAM 認証: app runtime の接続に、接続ごとの auth token を差し込む。

password を持たない接続文字列 (AWS) では認証情報を接続のたびに IAM から作る。
token は 15 分で失効するので engine 生成時に 1 度作るのでは足りず、asyncpg の
callable password (接続確立ごとに呼ばれ、awaitable なら await される) に載せる。

``generate_db_auth_token`` 自体は SigV4 のローカル署名で I/O を持たない。ただし client
生成が credential を先に解決する (ECS の credential endpoint への HTTP) ので、同期の
I/O が 2 箇所ある。どちらも ``asyncio.to_thread`` の中に閉じて event loop を塞がない。

aiobotocore は採らない。botocore を狭い範囲に pin して更新を律速する一方、得られる
非同期化はこの用途 (ローカル署名 + 接続ごとの低頻度) では to_thread と変わらない。
AWS API を非同期で多数呼ぶ要件が出た時点で見直す。

region は settings から明示的に渡す。**botocore が region に読む env は
``AWS_DEFAULT_REGION`` だけで、ECS が注入する ``AWS_REGION`` は見ない**ため、解決規則に
任せると本番の全 task が engine 生成で ``NoRegionError`` になる。credential は
解決規則のまま (ECS では task role)。

**射程は app runtime の接続だけ。** migration と運用スクリプトは ``create_app_engine``
を直接使い、migrator role の password 認証を続ける。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

import botocore.session
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db_ssl import create_app_engine

# token は host:port ごとに署名するため、URL に port が無い場合の既定が要る。
_DEFAULT_POSTGRES_PORT = 5432


@lru_cache(maxsize=1)
def _rds_client(region: str) -> Any:
    """プロセス内で 1 つだけ持つ rds client。

    ``get_session()`` は毎回新しい Session を返すため、engine ごとに作ると service
    model の読み込みが重複する (実測: 1 本目 +18.5MB、新 session で 3 本 +34.0MB)。
    engine を 4 本持つ worker-analysis では 50MB 超になり、実測で詰めたサイズ表の
    余裕を無視できない幅で食う。credential も session ごとに独立に取得・更新する。

    生成は初回の token 要求まで遅れるので、「engine は fork 後に作る」方針も崩さない。
    """
    return botocore.session.get_session().create_client("rds", region_name=region)


def build_iam_password_provider(
    url: str, *, region: str
) -> Callable[[], Awaitable[str]]:
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

    def generate() -> str:
        return _rds_client(region).generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=user
        )

    async def provide() -> str:
        return await asyncio.to_thread(generate)

    return provide


def create_runtime_engine(url: str, **engine_kwargs: Any) -> AsyncEngine:
    """app runtime の engine を作る。IAM 認証が有効なら token provider を差し込む。

    無効時は ``create_app_engine`` と同じで、URL の password がそのまま使われる。
    migration / 運用スクリプトはこの関数を使わない (上の射程の注記)。
    """
    if not settings.db_iam_auth:
        return create_app_engine(url, **engine_kwargs)
    region = settings.aws_region
    if region is None:
        # config の _require_region_when_iam_auth が保証するが、型を絞るために残す。
        raise RuntimeError("AWS_REGION is required when DB_IAM_AUTH is enabled")
    provider = build_iam_password_provider(url, region=region)
    return create_app_engine(url, password_provider=provider, **engine_kwargs)
