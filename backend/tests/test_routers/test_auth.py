"""Tests for /api/v1/auth router endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.auth_service import _hash_token, hash_password

TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "SecurePass123!"


async def _create_user(session: AsyncSession, email: str = TEST_EMAIL) -> User:
    """Helper to create a test user."""
    user = User(
        email=email,
        hashed_password=hash_password(TEST_PASSWORD),
        display_name="Test User",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.mark.asyncio
class TestRegister:
    async def test_register_success(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": TEST_EMAIL,
                "password": TEST_PASSWORD,
                "displayName": "Test User",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == TEST_EMAIL
        assert data["displayName"] == "Test User"
        assert data["isActive"] is True
        assert "id" in data
        assert "createdAt" in data

    async def test_register_duplicate_email(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_user(db_session)
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 409

    async def test_register_invalid_email(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": TEST_PASSWORD},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestLogin:
    async def test_login_success(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_user(db_session)
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "accessToken" in data
        assert "refreshToken" in data
        assert data["tokenType"] == "bearer"

    async def test_login_wrong_password(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_user(db_session)
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": "WrongPassword!"},
        )
        assert resp.status_code == 401

    async def test_login_nonexistent_user(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "noone@example.com", "password": TEST_PASSWORD},
        )
        assert resp.status_code == 401

    async def test_login_returns_valid_jwt(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_user(db_session)
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        token = resp.json()["accessToken"]

        # Use the token to access a protected resource
        health_resp = await client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert health_resp.status_code == 200


@pytest.mark.asyncio
class TestRefresh:
    async def test_refresh_success(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_user(db_session)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        refresh_token = login_resp.json()["refreshToken"]

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": refresh_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "accessToken" in data
        assert "refreshToken" in data
        # New refresh token should be different (rotated)
        assert data["refreshToken"] != refresh_token

    async def test_refresh_grace_period_allows_concurrent_reuse(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Reuse within the grace period should succeed (concurrent request)."""
        await _create_user(db_session)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        old_refresh = login_resp.json()["refreshToken"]

        # First refresh: rotates the token, sets revoked_at
        first = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert first.status_code == 200

        # Second refresh with old token within grace period: should succeed
        second = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert second.status_code == 200
        assert second.json()["refreshToken"] != first.json()["refreshToken"]

    async def test_refresh_outside_grace_period_revokes_all(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reuse outside the grace period triggers full token revocation."""
        from app.config import settings

        monkeypatch.setattr(settings, "jwt_refresh_grace_period_seconds", 0)

        await _create_user(db_session)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        old_refresh = login_resp.json()["refreshToken"]

        # First refresh: rotates the token
        first = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert first.status_code == 200
        new_refresh = first.json()["refreshToken"]

        # Reuse old token with grace period=0: should fail
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert resp.status_code == 401

        # The new token from the first refresh should also be revoked
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": new_refresh},
        )
        assert resp2.status_code == 401

    async def test_refresh_grace_period_multiple_concurrent(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Three concurrent reuses within grace period should all succeed."""
        await _create_user(db_session)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        old_refresh = login_resp.json()["refreshToken"]

        # First refresh: rotates the token
        await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )

        # Second and third reuses within grace period
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert resp2.status_code == 200

        resp3 = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert resp3.status_code == 200

    async def test_refresh_revoked_without_revoked_at_rejects(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Pre-migration tokens with is_revoked=True but revoked_at=None
        should be rejected without triggering grace period logic."""
        await _create_user(db_session)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        old_refresh = login_resp.json()["refreshToken"]

        # Manually revoke without setting revoked_at (simulates pre-migration data)
        token_hash = _hash_token(old_refresh)
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        result = await db_session.execute(stmt)
        db_token = result.scalar_one()
        db_token.is_revoked = True
        db_token.revoked_at = None
        db_session.add(db_token)
        await db_session.commit()

        # Should be rejected (revoked_at is None → no grace period)
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert resp.status_code == 401

    async def test_refresh_invalid_token(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": "totally-invalid-token"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestLogout:
    async def test_logout_revokes_token(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_user(db_session)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        refresh_token = login_resp.json()["refreshToken"]

        # Logout
        resp = await client.post(
            "/api/v1/auth/logout",
            json={"refreshToken": refresh_token},
        )
        assert resp.status_code == 204

        # Try to use the revoked refresh token
        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": refresh_token},
        )
        assert refresh_resp.status_code == 401


@pytest.mark.asyncio
class TestProtectedEndpoint:
    async def test_no_token_returns_401(self, client: AsyncClient) -> None:
        """Endpoints using get_current_user should return 401 without auth."""
        resp = await client.get("/api/v1/keywords")
        assert resp.status_code == 401

    async def test_health_does_not_require_auth(self, client: AsyncClient) -> None:
        """Health endpoint should work without authentication."""
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
