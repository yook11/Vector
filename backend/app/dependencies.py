from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt.exceptions import InvalidTokenError
from opentelemetry import trace

from app.config import settings

# BFF (Next.js) と backend (FastAPI) 間の内部 API 認証は HS256 JWT で行う。
# BFF が署名した短期 JWT を、backend は同じ secret で検証する。
# 発行から60秒の有効期限は、漏洩した個々の JWT の再利用可能期間を短くする。
# 署名鍵が漏洩した場合は鍵を交換し、検証側で旧鍵の受け入れを停止する必要がある。
_JWT_ALGORITHM = "HS256"
# iss / aud は frontend と揃え、発行元・利用先の異なる JWT の受け入れを防ぐ。
# 署名鍵を持つ攻撃者は iss / aud も指定できるため、鍵漏洩への防御にはならない。
_JWT_ISSUER = "vector-bff"
_JWT_AUDIENCE = "vector-backend"


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """BFF が署名した内部 JWT の claim から構築する軽量なユーザー表現。"""

    id: UUID
    role: UserRole


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
    # PyJWT は exp などを既定では必須にしないため、decode 層で BFF 文脈の
    # claim (exp/iat/iss/aud) の存在をまとめて要求する。sub/role はここでは
    # 要求せず、user 依存の dependency が _user_from_claims で強制する
    # (user-less な BFF 経由証明トークンも decode 可能にするため)。
    try:
        return jwt.decode(
            token,
            settings.bff_jwt_signing_secret.get_secret_value(),
            algorithms=[_JWT_ALGORITHM],
            audience=_JWT_AUDIENCE,
            issuer=_JWT_ISSUER,
            options={
                "require": ["exp", "iat", "iss", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except InvalidTokenError:
        return None


def _user_from_claims(payload: dict[str, object]) -> AuthenticatedUser | None:
    """JWT claim から AuthenticatedUser を組み立てる。claim 不正なら None。"""
    sub = payload.get("sub")
    role = payload.get("role")
    if not isinstance(sub, str) or not isinstance(role, str):
        return None
    try:
        return AuthenticatedUser(id=UUID(sub), role=UserRole(role))
    except ValueError:
        return None


async def require_bff_request(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """ユーザー情報の有無によらず BFF の署名を検証し、検証済み claims を返す。"""
    payload = _decode_internal_jwt(authorization)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return payload


async def require_authenticated_user(
    payload: Annotated[dict[str, object], Depends(require_bff_request)],
) -> AuthenticatedUser:
    """BFF が証明したユーザーを要求し、sub または role が不正なら 401 を返す。"""
    user = _user_from_claims(payload)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    # instrument_fastapi の request span (extra_spans=False のため current span)
    # に user を紐付ける。enduser.id (OTel semconv) には内部 UUID のみ載せる
    # (email 等の PII 不可)。span が無い文脈では no-op。
    trace.get_current_span().set_attribute("enduser.id", str(user.id))
    return user


async def require_admin_user(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> AuthenticatedUser:
    """現在のユーザーが admin ロールを持つことを要求する。持たない場合は 403。"""
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
