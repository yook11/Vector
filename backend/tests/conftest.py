"""Shared test fixtures for backend tests."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.dependencies import get_session
from app.main import app
from app.models import AnalysisResult, Keyword, NewsArticle, NewsKeyword, RefreshToken, User, UserKeywordSubscription, WatchlistItem  # noqa: F401
from app.services.auth_service import create_access_token

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/vector_test"
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)


@pytest.fixture(scope="session", autouse=True)
async def ensure_test_database() -> None:
    """Create vector_test database if it doesn't exist."""
    base_url = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    engine = create_async_engine(
        base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'vector_test'")
        )
        if not result.scalar():
            await conn.execute(text("CREATE DATABASE vector_test"))
    await engine.dispose()


@pytest.fixture(autouse=True)
async def setup_db(ensure_test_database: None) -> AsyncGenerator[None, None]:
    """Create tables before each test, drop after."""
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a test database session."""
    async with SQLModelAsyncSession(
        engine_test, expire_on_commit=False
    ) as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient with DI-overridden session."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create and return a test user."""
    from app.services.auth_service import hash_password

    user = User(
        email="test@example.com",
        hashed_password=hash_password("TestPass123"),
        display_name="Test User",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def auth_headers(test_user: User) -> dict[str, str]:
    """Return Authorization headers with a valid JWT for test_user."""
    token = create_access_token(test_user.id, test_user.email)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def authed_client(
    db_session: AsyncSession, test_user: User
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient with auth headers pre-set."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    token = create_access_token(test_user.id, test_user.email)
    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def sample_keyword(db_session: AsyncSession) -> Keyword:
    """Create and return a test keyword."""
    kw = Keyword(keyword="Quantum Computing", category="computing", is_active=True)
    db_session.add(kw)
    await db_session.commit()
    await db_session.refresh(kw)
    return kw
