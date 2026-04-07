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
    """Lightweight user representation populated from BFF proxy headers."""

    id: UUID
    role: UserRole


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SQLModelAsyncSession(engine) as session:
        yield session


async def get_current_user(
    x_user_id: Annotated[UUID, Header()],
    x_user_role: Annotated[UserRole, Header()],
    x_internal_secret: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """Validate X-Internal-Secret and extract user from BFF proxy headers.

    Required headers: X-User-ID (UUID), X-User-Role (user|admin).
    Missing or invalid values return 422 (FastAPI type validation).
    Invalid secret returns 401.
    """
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return CurrentUser(id=x_user_id, role=x_user_role)


async def get_admin_user(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Require the current user to have admin role. Raises 403 if not."""
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
    """Return CurrentUser if authenticated, None otherwise.

    All headers are optional. Invalid UUID/Role values return 422
    (FastAPI type validation). X-User-ID present without X-User-Role
    is a BFF bug and returns 401.
    """
    if x_internal_secret != settings.internal_api_secret:
        return None
    if x_user_id is None:
        return None
    if x_user_role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return CurrentUser(id=x_user_id, role=x_user_role)
