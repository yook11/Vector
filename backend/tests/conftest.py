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
from app.models import (  # noqa: F401
    AIModel,
    AnalysisInvestmentCategory,
    ArticleGroup,
    FetchLog,
    AnalysisResult,
    AnalysisTranslation,
    InvestmentCategory,
    InvestmentCategoryTranslation,
    Keyword,
    KeywordCategory,
    KeywordCategoryLink,
    KeywordCategoryTranslation,
    NewsArticle,
    NewsKeyword,
    NewsSource,
    RefreshToken,
    User,
    UserKeywordSubscription,
    WatchlistItem,
)
from app.services.auth_service import create_access_token

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/vector_test"
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)


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

    # Enable pgvector extension in the test database
    async with engine_test.connect() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.commit()


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
    async with SQLModelAsyncSession(engine_test, expire_on_commit=False) as session:
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
async def sample_ai_model(db_session: AsyncSession) -> AIModel:
    """Create and return a test AI model."""
    model = AIModel(provider="gemini", name="gemini-2.0-flash")
    db_session.add(model)
    await db_session.commit()
    await db_session.refresh(model)
    return model


@pytest.fixture
async def sample_keyword(db_session: AsyncSession) -> Keyword:
    """Create and return a test keyword."""
    kw = Keyword(keyword="Quantum Computing")
    db_session.add(kw)
    await db_session.commit()
    await db_session.refresh(kw)
    return kw


@pytest.fixture
async def sample_source(db_session: AsyncSession) -> NewsSource:
    """Create and return a test RSS news source."""
    source = NewsSource(
        name="Test Tech Source",
        source_type="rss",
        feed_url="https://example.com/feed.xml",
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
        source_type="api",
        api_endpoint="hacker-news",
        site_url="https://news.ycombinator.com",
        is_active=True,
        fetch_interval_minutes=360,
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
        source_type="api",
        api_endpoint="alpha-vantage",
        site_url="https://www.alphavantage.co",
        is_active=True,
        fetch_interval_minutes=1440,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def sample_keyword_categories(
    db_session: AsyncSession,
) -> list[KeywordCategory]:
    """Create and return sample keyword categories with translations."""
    seed = [
        ("ai_ml", "AI・ML", "AI & ML"),
        ("quantum", "量子コンピュータ", "Quantum Computing"),
        ("semiconductor", "半導体", "Semiconductor"),
    ]
    categories: list[KeywordCategory] = []
    for slug, name_ja, name_en in seed:
        cat = KeywordCategory(slug=slug)
        db_session.add(cat)
        await db_session.flush()
        db_session.add(
            KeywordCategoryTranslation(category_id=cat.id, locale="ja", name=name_ja)
        )
        db_session.add(
            KeywordCategoryTranslation(category_id=cat.id, locale="en", name=name_en)
        )
        categories.append(cat)
    await db_session.commit()
    for cat in categories:
        await db_session.refresh(cat)
    return categories


@pytest.fixture
async def sample_categories(
    db_session: AsyncSession,
) -> list[InvestmentCategory]:
    """Create and return the 6 standard investment categories with translations."""
    seed = [
        (
            "competitive_edge",
            "競争優位",
            "Competitive Edge",
            "技術突破、特許取得、市場シェア拡大",
        ),
        (
            "financial_signal",
            "業績シグナル",
            "Financial Signal",
            "決算、売上変化、利益率、資金調達",
        ),
        (
            "growth_catalyst",
            "成長期待",
            "Growth Catalyst",
            "新製品、市場拡大、提携など成長を示唆するニュース",
        ),
        (
            "market_disruption",
            "市場破壊",
            "Market Disruption",
            "新技術による既存市場への脅威、業界再編",
        ),
        (
            "regulatory_shift",
            "規制変化",
            "Regulatory Shift",
            "新法規、政策変更、補助金、輸出規制",
        ),
        (
            "risk_mitigation",
            "リスク回避",
            "Risk Mitigation",
            "訴訟勝訴、規制クリア、安全性確認など",
        ),
    ]
    categories: list[InvestmentCategory] = []
    for slug, name_ja, name_en, desc in seed:
        cat = InvestmentCategory(slug=slug)
        db_session.add(cat)
        await db_session.flush()
        db_session.add(
            InvestmentCategoryTranslation(
                category_id=cat.id,
                locale="ja",
                name=name_ja,
                description=desc,
            )
        )
        db_session.add(
            InvestmentCategoryTranslation(
                category_id=cat.id,
                locale="en",
                name=name_en,
                description=desc,
            )
        )
        categories.append(cat)
    await db_session.commit()
    for cat in categories:
        await db_session.refresh(cat)
    return categories
