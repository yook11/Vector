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

本moduleはapp runtimeの設定adapterだけを持つ。migrationは設定非依存の
``app.rds_iam_auth``を同じく利用する。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db_ssl import create_app_engine
from app.rds_iam_auth import _rds_client, build_iam_password_provider

__all__ = ["_rds_client", "build_iam_password_provider", "create_runtime_engine"]


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
