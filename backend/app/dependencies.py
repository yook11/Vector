from collections.abc import AsyncGenerator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.db import engine


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Lightweight user representation populated from BFF proxy headers."""

    id: str
    role: str


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

    role = request.headers.get("X-User-Role", "user")
    return CurrentUser(id=user_id, role=role)


async def get_admin_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Require the current user to have admin role. Raises 403 if not."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


async def get_optional_user(request: Request) -> CurrentUser | None:
    """Like get_current_user but returns None instead of raising 401."""
    secret = request.headers.get("X-Internal-Secret")
    if secret != settings.internal_api_secret:
        return None

    user_id = request.headers.get("X-User-ID")
    if not user_id:
        return None

    role = request.headers.get("X-User-Role", "user")
    return CurrentUser(id=user_id, role=role)
