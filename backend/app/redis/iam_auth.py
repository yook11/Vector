"""ElastiCache IAM 認証: Valkey 接続に、接続ごとの auth token を差し込む。

RDS (``app/db/iam.py``) と同型だが、ElastiCache には ``generate_db_auth_token``
に相当する公式 helper が無く、token は SigV4 presign の自前組み立てになる。
``https://<cache名>/?Action=connect&User=<user>`` を service=elasticache で presign
し、scheme を落とした残り全体を AUTH の password として渡す。**署名の host は
DNS endpoint ではなく cache 名 (replication group id)** で、URL からは導出できない
ため settings で明示的に受ける。

token は 15 分で失効し、IAM 認証の接続は 12 時間で強制切断される。redis-py の
``CredentialProvider`` は新規接続・再接続のたびに呼ばれるため、その口に載せる
ことで「切断 → 自動再接続 → 新 token」が成立する。token はキャッシュしない:
署名はローカル計算で安く、キャッシュは期限際の再接続に失効 token を掴ませる
窓を作るだけになる。

region は settings から明示的に渡す (botocore が ``AWS_REGION`` を読まない事情は
``app/db/iam.py`` の注記と同じ)。
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from redis.credentials import CredentialProvider

# AWS 側の token 有効期限が 15 分のため、presign もそれに合わせる。
_TOKEN_EXPIRES_IN_SECONDS = 900


@lru_cache(maxsize=1)
def _botocore_session() -> Any:
    """プロセス内で 1 本だけ持つ botocore session。

    RequestSigner は session を強参照せず、session が GC されると event_emitter
    内の weakref が死んで以後の再接続の token 生成が全て ``ReferenceError`` に
    なるため、cache で signer と同寿命に生かし続ける。
    """
    import botocore.session

    return botocore.session.get_session()


@lru_cache(maxsize=1)
def _request_signer(region: str) -> Any:
    """プロセス内で 1 つだけ持つ signer。

    session ごとに credential を独立に取得・更新するため、複数brokerがproviderを
    持つ worker でも解決を 1 本に共有する (``app/db/iam.py`` の ``_rds_client``
    と同じ判断)。botocore の import と credential 解決 (ECS では HTTP) も初回の
    token 要求まで遅れる。
    """
    from botocore.model import ServiceId
    from botocore.signers import RequestSigner

    session = _botocore_session()
    return RequestSigner(
        ServiceId("elasticache"),
        region,
        "elasticache",
        "v4",
        session.get_credentials(),
        session.get_component("event_emitter"),
    )


class ElastiCacheIAMProvider(CredentialProvider):
    """接続確立ごとに SigV4 presign の auth token を作る provider。"""

    def __init__(self, *, user: str, cache_name: str, region: str) -> None:
        self.user = user
        self.cache_name = cache_name
        self.region = region

    def get_credentials(self) -> tuple[str, str]:
        url = urlunparse(
            (
                "https",
                self.cache_name,
                "/",
                "",
                urlencode({"Action": "connect", "User": self.user}),
                "",
            )
        )
        signed: str = _request_signer(self.region).generate_presigned_url(
            {"method": "GET", "url": url, "body": {}, "headers": {}, "context": {}},
            operation_name="connect",
            expires_in=_TOKEN_EXPIRES_IN_SECONDS,
            region_name=self.region,
        )
        return self.user, signed.removeprefix("https://")

    async def get_credentials_async(self) -> tuple[str, str]:
        # 署名はローカル計算だが、初回だけ credential 解決の同期 I/O があるため
        # event loop を塞がない。base class の実装は毎回 warning を出すので override。
        return await asyncio.to_thread(self.get_credentials)


def redis_connection_options(
    url: str,
    *,
    iam_auth: bool,
    region: str | None,
    cache_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """接続 URL と、そこに足す接続 kwargs を返す。

    IAM 認証が有効なら「どの user で繋ぐか」を URL から provider へ写し、userinfo を
    除いた URL を返す。redis-py は URL の username と ``credential_provider`` の併用を
    ``DataError`` で拒否するため、URL を単一の情報源にしてここで分離する。
    無効時は URL をそのまま使う (dev / Fly の password 認証)。
    """
    if not iam_auth:
        return url, {}
    if region is None or cache_name is None:
        # config の validator が保証するが、型を絞るために残す。
        raise RuntimeError(
            "AWS_REGION and REDIS_IAM_CACHE_NAME are required "
            "when REDIS_IAM_AUTH is enabled"
        )
    parsed = urlparse(url)
    user = parsed.username
    if user is None:
        raise ValueError(
            "an ElastiCache auth token requires a user in the connection URL "
            "(the token is signed per cache user)"
        )
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    provider = ElastiCacheIAMProvider(user=user, cache_name=cache_name, region=region)
    return urlunparse(parsed._replace(netloc=netloc)), {"credential_provider": provider}
