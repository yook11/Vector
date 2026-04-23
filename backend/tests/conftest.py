"""バックエンドテスト共通のフィクスチャ。"""

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
    Article,
    ArticleAnalysis,
    ArticleEntity,
    ArticleExtraction,
    ArticleRejection,
    Category,
    DiscoveredArticle,
    FetchLog,
    ImpactLevel,
    NewsSource,
    SourceType,
    Topic,
    WatchlistEntry,
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/vector_test"
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

# --- BFF header-based auth helpers ---

TEST_USER_ID = "00000000-0000-4000-a000-000000000001"
TEST_ADMIN_ID = "00000000-0000-4000-a000-000000000002"
INTERNAL_SECRET = settings.internal_api_secret.get_secret_value()


def _auth_headers(user_id: str, role: str = "user") -> dict[str, str]:
    """テスト用に X-User-ID / X-User-Role / X-Internal-Secret ヘッダーを組み立てる。"""
    return {
        "X-User-ID": user_id,
        "X-User-Role": role,
        "X-Internal-Secret": INTERNAL_SECRET,
    }


@pytest.fixture(scope="session", autouse=True)
async def ensure_test_database() -> None:
    """vector_test DB が無ければ作成し、pgvector を有効化する。"""
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

    # テスト DB に pgvector 拡張と auth スキーマを用意する
    async with engine_test.connect() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
        await conn.commit()


@pytest.fixture(autouse=True)
async def setup_db(ensure_test_database: None) -> AsyncGenerator[None, None]:
    """各テスト前にテーブルを作成し、終了後に破棄する。"""
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # watchlist_entries.user_id の FK を満たすため auth.user を seed する
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
    """Service クラスのテスト用に session factory を提供する。"""
    return async_sessionmaker(
        engine_test,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """テスト用 DB セッションを提供する。"""
    async with SQLModelAsyncSession(engine_test, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """DI でセッションを差し替えた httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # db_session には autobegin されたトランザクションが残ることがある
        # (seed の refresh など)。本番の get_session と同様に新しい
        # トランザクションを開始するため、ここで一度 commit しておく。
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
    """通常ユーザー用の BFF プロキシ認証ヘッダーを返す。"""
    return _auth_headers(TEST_USER_ID)


@pytest.fixture
async def authed_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """BFF プロキシ認証ヘッダーを付与済みの httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # db_session には autobegin されたトランザクションが残ることがある
        # (seed の refresh など)。本番の get_session と同様に新しい
        # トランザクションを開始するため、ここで一度 commit しておく。
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
    """管理者用 BFF プロキシ認証ヘッダーを付与済みの httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # db_session には autobegin されたトランザクションが残ることがある
        # (seed の refresh など)。本番の get_session と同様に新しい
        # トランザクションを開始するため、ここで一度 commit しておく。
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
    """サンプルカテゴリを作成して返す (name は通常のカラム)。"""
    seed = [
        ("ai", "AI"),
        ("computing", "次世代コンピューティング"),
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
async def sample_topic(
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> Topic:
    """テスト用トピックを作成して返す（カテゴリが必須）。"""
    topic = Topic(
        name="quantum computing",
        label_ja="量子コンピューティング",
        category_id=sample_categories[1].id,
    )
    db_session.add(topic)
    await db_session.commit()
    await db_session.refresh(topic)
    return topic


@pytest.fixture
async def sample_source(db_session: AsyncSession) -> NewsSource:
    """テスト用 RSS ニュースソースを作成して返す。"""
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
    """テスト用 Hacker News API ソースを作成して返す。"""
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
    """テスト用 Alpha Vantage API ソースを作成して返す。"""
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
