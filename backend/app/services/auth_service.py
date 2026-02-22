"""Authentication service — password hashing, JWT management, refresh token rotation."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import structlog
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

logger = structlog.get_logger(__name__)


# --- Password utilities ---


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


# --- JWT utilities ---


def create_access_token(user_id: int, email: str) -> str:
    """Create a signed JWT access token."""
    expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token.

    Raises JWTError if the token is invalid, expired, or not an access token.
    """
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != "access":
        raise JWTError("Not an access token")
    return payload


# --- Refresh token utilities ---


def _generate_refresh_token() -> str:
    """Generate a cryptographically secure random refresh token."""
    return secrets.token_urlsafe(64)


def _hash_token(token: str) -> str:
    """Hash a refresh token for safe DB storage."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_refresh_token(session: AsyncSession, user_id: int) -> str:
    """Create a new refresh token, store its hash in DB, return the raw token."""
    raw_token = _generate_refresh_token()
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(days=settings.jwt_refresh_expire_days)

    db_token = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(db_token)
    await session.commit()

    logger.info("refresh_token_created", user_id=user_id)
    return raw_token


async def rotate_refresh_token(
    session: AsyncSession, raw_token: str
) -> tuple[str, int]:
    """Validate and rotate a refresh token.

    Revokes the old token and issues a new one.
    Returns (new_raw_token, user_id).
    Raises ValueError if the token is invalid, expired, or already revoked.
    """
    token_hash = _hash_token(raw_token)
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    result = await session.execute(stmt)
    db_token = result.scalar_one_or_none()

    if db_token is None:
        raise ValueError("Invalid refresh token")

    if db_token.is_revoked:
        # Allow concurrent requests within the grace period
        if (
            db_token.revoked_at is not None
            and (datetime.now(UTC) - db_token.revoked_at).total_seconds()
            < settings.jwt_refresh_grace_period_seconds
        ):
            new_raw_token = await create_refresh_token(session, db_token.user_id)
            logger.info("refresh_token_grace_period_reuse", user_id=db_token.user_id)
            return new_raw_token, db_token.user_id
        # Outside grace period — genuine reuse attack
        await _revoke_all_user_tokens(session, db_token.user_id)
        raise ValueError("Refresh token already revoked")

    if db_token.expires_at < datetime.now(UTC):
        raise ValueError("Refresh token expired")

    # Revoke the old token
    db_token.is_revoked = True
    db_token.revoked_at = datetime.now(UTC)
    session.add(db_token)

    # Issue a new token
    new_raw_token = await create_refresh_token(session, db_token.user_id)

    logger.info("refresh_token_rotated", user_id=db_token.user_id)
    return new_raw_token, db_token.user_id


async def revoke_refresh_token(session: AsyncSession, raw_token: str) -> None:
    """Revoke a specific refresh token (used on logout)."""
    token_hash = _hash_token(raw_token)
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    result = await session.execute(stmt)
    db_token = result.scalar_one_or_none()

    if db_token and not db_token.is_revoked:
        db_token.is_revoked = True
        # Do NOT set revoked_at — only rotation sets it to enable grace period.
        # Logout-revoked tokens must never be reusable.
        session.add(db_token)
        await session.commit()
        logger.info("refresh_token_revoked", user_id=db_token.user_id)


async def _revoke_all_user_tokens(session: AsyncSession, user_id: int) -> None:
    """Revoke all refresh tokens for a user (security measure)."""
    stmt = select(RefreshToken).where(
        RefreshToken.user_id == user_id,
        RefreshToken.is_revoked == False,  # noqa: E712
    )
    result = await session.execute(stmt)
    tokens = result.scalars().all()
    for token in tokens:
        token.is_revoked = True
        # Do NOT set revoked_at — only rotation sets it to enable grace period.
        # Security-revoked tokens must never be reusable.
        session.add(token)
    await session.commit()
    logger.warning("all_refresh_tokens_revoked", user_id=user_id, count=len(tokens))


# --- User operations ---


async def create_user(
    session: AsyncSession,
    email: str,
    password: str,
    display_name: str | None = None,
) -> User:
    """Create a new user. Raises ValueError if email is already taken."""
    existing = await session.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise ValueError("Email already registered")

    user = User(
        email=email,
        hashed_password=hash_password(password),
        display_name=display_name,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    logger.info("user_created", user_id=user.id, email=email)
    return user


async def authenticate_user(
    session: AsyncSession, email: str, password: str
) -> User | None:
    """Validate credentials and return the user, or None if invalid."""
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(password, user.hashed_password):
        return None

    if not user.is_active:
        return None

    return user


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Look up a user by ID."""
    return await session.get(User, user_id)
