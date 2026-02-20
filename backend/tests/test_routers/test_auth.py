"""Tests for /api/v1/auth router endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.auth_service import hash_password

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

    async def test_refresh_reuse_revoked_token(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
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

        # Second refresh with old token: should fail (reuse detection)
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
