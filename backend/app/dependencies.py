from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.db import engine


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Lightweight user representation populated from BFF proxy headers."""

    id: UUID
    role: UserRole


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SQLModelAsyncSession(engine) as session:
        yield session


async def get_current_user(request: Request) -> CurrentUser:
    """Validate X-Internal-Secret and extract user from BFF proxy headers.

    Returns CurrentUser or raises 401.
    """
    secret = request.headers.get("X-Internal-Secret")
    if secret != settings.internal_api_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        parsed_id = UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID format",
        )

    raw_role = request.headers.get("X-User-Role", UserRole.USER)
    try:
        role = UserRole(raw_role)
    except ValueError:
        role = UserRole.USER
    return CurrentUser(id=parsed_id, role=role)


async def get_admin_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Require the current user to have admin role. Raises 403 if not."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


async def get_optional_user(request: Request) -> CurrentUser | None:
    """Like get_current_user but returns None instead of raising 401.

    ただし Secret が正しく X-User-ID が存在するのに UUID として不正な場合は
    BFF のバグなので 401 を返す（バグを握りつぶさない）。
    """
    secret = request.headers.get("X-Internal-Secret")
    if secret != settings.internal_api_secret:
        return None

    user_id = request.headers.get("X-User-ID")
    if not user_id:
        return None

    try:
        parsed_id = UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID format",
        )

    raw_role = request.headers.get("X-User-Role", UserRole.USER)
    try:
        role = UserRole(raw_role)
    except ValueError:
        role = UserRole.USER
    return CurrentUser(id=parsed_id, role=role)
