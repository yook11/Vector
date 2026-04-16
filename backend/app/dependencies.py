import secrets
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.db import engine


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """BFF プロキシヘッダから構築する軽量なユーザー表現。"""

    id: UUID
    role: UserRole


async def get_session() -> AsyncGenerator[AsyncSession, None]:
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


async def get_current_user(
    x_user_id: Annotated[UUID, Header()],
    x_user_role: Annotated[UserRole, Header()],
    x_internal_secret: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """X-Internal-Secret を検証し、BFF プロキシヘッダからユーザーを取り出す。

    必須ヘッダ: X-User-ID (UUID), X-User-Role (user|admin)。
    ヘッダが欠落・型不正の場合は 422（FastAPI の型バリデーション）。
    シークレット不一致の場合は 401。
    """
    if not x_internal_secret or not secrets.compare_digest(
        x_internal_secret, settings.internal_api_secret.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return CurrentUser(id=x_user_id, role=x_user_role)


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


async def get_optional_user(
    x_internal_secret: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[UUID | None, Header()] = None,
    x_user_role: Annotated[UserRole | None, Header()] = None,
) -> CurrentUser | None:
    """認証済みなら CurrentUser を返し、そうでなければ None を返す。

    すべてのヘッダは任意。UUID や Role の値が不正なら 422（FastAPI の
    型バリデーション）。X-User-ID はあるのに X-User-Role が無い場合は
    BFF 側のバグなので 401 を返す。
    """
    if not x_internal_secret or not secrets.compare_digest(
        x_internal_secret, settings.internal_api_secret.get_secret_value()
    ):
        return None
    if x_user_id is None:
        return None
    if x_user_role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return CurrentUser(id=x_user_id, role=x_user_role)
