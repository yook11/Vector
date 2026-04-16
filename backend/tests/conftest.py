"""Shared test fixtures for backend tests."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.dependencies import get_session
from app.main import app
from app.models import (  # noqa: F401
    ArticleAnalysis,
    ArticleKeyword,
    Category,
    FetchLog,
    ImpactLevel,
    Keyword,
    NewsArticle,
    NewsSource,
    SourceType,
    WatchlistEntry,
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/vector_test"
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

# --- BFF header-based auth helpers ---

TEST_USER_ID = "00000000-0000-4000-a000-000000000001"
TEST_ADMIN_ID = "00000000-0000-4000-a000-000000000002"
INTERNAL_SECRET = settings.internal_api_secret.get_secret_value()


def _auth_headers(user_id: str, role: str = "user") -> dict[str, str]:
    """Build X-User-ID / X-User-Role / X-Internal-Secret headers for tests."""
    return {
        "X-User-ID": user_id,
        "X-User-Role": role,
        "X-Internal-Secret": INTERNAL_SECRET,
    }


@pytest.fixture(scope="session", autouse=True)
async def ensure_test_database() -> None:
    """Create vector_test database if it doesn't exist, and enable pgvector."""
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

    # Enable pgvector extension and create auth schema in the test database
    async with engine_test.connect() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
        await conn.commit()


@pytest.fixture(autouse=True)
async def setup_db(ensure_test_database: None) -> AsyncGenerator[None, None]:
    """Create tables before each test, drop after."""
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # Seed auth.user rows so FK on watchlist_entries.user_id is satisfied
        await conn.execute(
            text(
                'INSERT INTO auth."user" (id) VALUES (:uid1), (:uid2) '
                "ON CONFLICT DO NOTHING"
            ),
            {"uid1": TEST_USER_ID, "uid2": TEST_ADMIN_ID},
        )
    yield
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)


@pytest.fixture
def session_factory() -> async_sessionmaker[AsyncSession]:
    """Provide a session factory for testing Service classes."""
    return async_sessionmaker(
        engine_test,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a test database session."""
    async with SQLModelAsyncSession(engine_test, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient with DI-overridden session."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # Fixtures may leave an open autobegin transaction on db_session
        # (e.g., from refresh after seed commits). Close it so the override
        # can open a fresh transaction that mirrors the production
        # get_session behavior.
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Return BFF proxy auth headers for a regular test user."""
    return _auth_headers(TEST_USER_ID)


@pytest.fixture
async def authed_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient with BFF proxy auth headers pre-set."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # Fixtures may leave an open autobegin transaction on db_session
        # (e.g., from refresh after seed commits). Close it so the override
        # can open a fresh transaction that mirrors the production
        # get_session behavior.
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_headers(TEST_USER_ID),
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient with admin BFF proxy auth headers pre-set."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # Fixtures may leave an open autobegin transaction on db_session
        # (e.g., from refresh after seed commits). Close it so the override
        # can open a fresh transaction that mirrors the production
        # get_session behavior.
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_headers(TEST_ADMIN_ID, role="admin"),
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def sample_categories(
    db_session: AsyncSession,
) -> list[Category]:
    """Create and return sample categories (name is a direct column)."""
    seed = [
        ("ai_ml", "AI・ML"),
        ("quantum", "量子コンピュータ"),
        ("semiconductor", "半導体"),
    ]
    categories: list[Category] = []
    for slug, name in seed:
        cat = Category(slug=slug, name=name)
        db_session.add(cat)
        categories.append(cat)
    await db_session.commit()
    for cat in categories:
        await db_session.refresh(cat)
    return categories


@pytest.fixture
async def sample_keyword(
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> Keyword:
    """Create and return a test keyword (requires a category)."""
    kw = Keyword(name="Quantum Computing", category_id=sample_categories[1].id)
    db_session.add(kw)
    await db_session.commit()
    await db_session.refresh(kw)
    return kw


@pytest.fixture
async def sample_source(db_session: AsyncSession) -> NewsSource:
    """Create and return a test RSS news source."""
    source = NewsSource(
        name="Test Tech Source",
        source_type=SourceType.RSS,
        site_url="https://example.com",
        endpoint_url="https://example.com/feed.xml",
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def sample_hn_source(db_session: AsyncSession) -> NewsSource:
    """Create and return a test Hacker News API source."""
    source = NewsSource(
        name="Hacker News",
        source_type=SourceType.API,
        site_url="https://news.ycombinator.com",
        endpoint_url="https://hn.algolia.com/api/v1/search_by_date",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def sample_av_source(db_session: AsyncSession) -> NewsSource:
    """Create and return a test Alpha Vantage API source."""
    source = NewsSource(
        name="Alpha Vantage",
        source_type=SourceType.API,
        site_url="https://www.alphavantage.co",
        endpoint_url="https://www.alphavantage.co/query",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source
