from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import jwt
import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, status
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.db import engine
from app.redis import get_redis as _get_redis_singleton

# BFF (Next.js) と backend (FastAPI) 間の内部 API 認証は HS256 JWT で行う。
# BFF が Better Auth セッションから user_id / role を取り出して短期 JWT に署名し、
# backend は同じ secret で検証する。BFF_JWT_SIGNING_SECRET 漏洩時の悪用ウィンドウを
# JWT 有効期限 (~60 秒) に限定するための構造。
_JWT_ALGORITHM = "HS256"
# iss / aud は frontend (`frontend/src/lib/api/internal-config.ts`) と
# 同じ literal を要求。secret 漏洩時に「Vector の文脈で署名された JWT」を
# 強制する二重防御 (red-team C2 / AUTH-N2)。
_JWT_ISSUER = "vector-bff"
_JWT_AUDIENCE = "vector-backend"


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """BFF が署名した内部 JWT の claim から構築する軽量なユーザー表現。"""

    id: UUID
    role: UserRole


async def get_session() -> AsyncGenerator[AsyncSession]:
    """トランザクション開始済みのセッションを yield する。

    正常終了時にコミット、例外発生時（Service が投げるドメイン例外を含む）
    はロールバックする。Repository はコミットや refresh を呼んではならない。
    ID の払い出しが必要なときのみ flush してよい。

    既存エンティティへの変更は、Service が明示的に save を呼ばなくても
    本トランザクションのコミット時に Unit of Work が自動で永続化する。
    新規作成・削除は Repository.create / Repository.delete 経由で行う。
    詳細は docs/adr/004_unit_of_work_service_convention.md を参照。
    """
    async with SQLModelAsyncSession(engine) as session:
        async with session.begin():
            yield session


def _decode_internal_jwt(authorization: str | None) -> dict[str, object] | None:
    """`Authorization: Bearer <jwt>` から claim dict を取り出す。

    署名不正・期限切れ・形式不正はすべて None で表現し、呼び出し側で 401 か
    None フォールバックかを判断する。"""
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    # 構造的厳格化 (red-team C2 / AUTH-N3 + AUTH-N2 対策):
    # - exp 不在を decode 層で reject (PyJWT/python-jose とも default 未要求)
    # - iss / aud 検証で BFF_JWT_SIGNING_SECRET 漏洩時の二重防御
    # - sub / role の必須化で `_user_from_claims` の事後 None チェックを decode 層に降格
    try:
        return jwt.decode(
            token,
            settings.bff_jwt_signing_secret.get_secret_value(),
            algorithms=[_JWT_ALGORITHM],
            audience=_JWT_AUDIENCE,
            issuer=_JWT_ISSUER,
            options={
                "require": ["exp", "iat", "sub", "role"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except InvalidTokenError:
        return None


def _user_from_claims(payload: dict[str, object]) -> CurrentUser | None:
    """JWT claim から CurrentUser を組み立てる。claim 不正なら None。"""
    sub = payload.get("sub")
    role = payload.get("role")
    if not isinstance(sub, str) or not isinstance(role, str):
        return None
    try:
        return CurrentUser(id=UUID(sub), role=UserRole(role))
    except ValueError:
        return None


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """`Authorization: Bearer <jwt>` を検証し CurrentUser を返す。

    BFF が HS256 で署名した短期 JWT を期待する。署名不正・期限切れ・
    claim 不正 (sub/role 欠落 or 値不正) はいずれも 401。"""
    payload = _decode_internal_jwt(authorization)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    user = _user_from_claims(payload)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


async def get_admin_user(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """現在のユーザーが admin ロールを持つことを要求する。持たない場合は 403。"""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


def get_redis_client() -> aioredis.Redis:
    """共有 Redis クライアントを返す FastAPI 依存関数。

    `app.redis.get_redis()` の薄いラッパで、目的はテスト時の override 取っ手。
    `app.dependency_overrides[get_redis_client] = lambda: fake_redis` で
    差し替え可能にするため、router/service は ``get_redis()`` を直接呼ばずに
    この Depends に依存する。
    """
    return _get_redis_singleton()


async def get_optional_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser | None:
    """認証済みなら CurrentUser を返し、そうでなければ None を返す。

    JWT が無い・署名不正・期限切れ・claim 不正のいずれも一律 None。
    認証必須エンドポイントとは異なり、未認証アクセスを許容する場面で使う。
    """
    if authorization is None:
        return None
    payload = _decode_internal_jwt(authorization)
    if payload is None:
        return None
    return _user_from_claims(payload)
